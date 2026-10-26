import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import desc, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine, SessionLocal
from app.models import Incident, RequestLog
from app.monitor import (
    start_monitor,
    stop_monitor,
    check_services_once,
    get_monitor_state,
    set_service_mode,
)
from app.request_validator import validate_request_data
from app.shield.controller import ShieldController
from app.shield.required_fields_guard import RequiredFieldsGuard

logger = logging.getLogger("chronoguard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_monitor()
    try:
        yield
    finally:
        stop_monitor()


app = FastAPI(
    title="ChronoGuard API",
    description="Self-Healing Dependency Guardian",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "null",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["*"],
)

shield = ShieldController()


class RequestValidationPayload(BaseModel):
    data: dict[str, Any]
    required_fields: list[str] = Field(min_length=1)


class AgeValidationPayload(BaseModel):
    age: Any


class SimulationPayload(BaseModel):
    event_type: str = Field(
        pattern="^(validRequest|badTimestamp|sourceDown|missingField|exception|retryLoop)$"
    )
    source: str = Field(pattern="^(client|app|primary|backup)$")
    shield_enabled: bool = True
    data: dict[str, Any] = Field(
        default_factory=lambda: {
            "name": "Demo User",
            "email": "demo@example.com",
        }
    )


def _record_request(**values) -> None:
    """Persist a request log to PostgreSQL."""
    db = SessionLocal()
    try:
        db.add(RequestLog(**values))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not persist ChronoGuard request log")
        raise
    finally:
        db.close()


def _record_incident(
    event_type: str,
    source: str,
    final_status: str,
    action: str,
) -> None:
    db = SessionLocal()
    try:
        db.add(
            Incident(
                incident_type=event_type,
                affected_service=source,
                severity="high" if final_status == "failed" else "medium",
                status=final_status,
                description=(
                    f"Simulation event '{event_type}' began at {source}."
                ),
                recovery_action=action,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not persist ChronoGuard incident")
    finally:
        db.close()


def _safe_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Exclude common secret fields from persisted payloads."""
    secret_names = (
        "password",
        "token",
        "secret",
        "authorization",
        "api_key",
    )
    return {
        key: value
        for key, value in data.items()
        if not any(secret in key.lower() for secret in secret_names)
    }


@app.get("/")
def root():
    return {
        "message": "ChronoGuard API is running",
        "status": "healthy",
        "version": "1.1.0",
    }


@app.get("/api/health")
def health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"api": "healthy", "database": "connected"}
    except Exception as exc:
        logger.exception("Health check failed")
        raise HTTPException(
            status_code=503,
            detail={"api": "running", "database": "disconnected"},
        ) from exc


@app.post("/api/shield/validate-request")
def validate_request(payload: RequestValidationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())

    # First check that required fields exist and are text.
    guard_result = RequiredFieldsGuard.validate(
        payload.data,
        payload.required_fields,
    )

    errors: list[str] = []
    validator_result = None

    if not guard_result["valid"]:
        errors.append(guard_result["error"])
    else:
        # Then validate email and timestamp according to project policy.
        validator_result = validate_request_data(payload.data)
        if not validator_result["valid"]:
            errors.extend(validator_result["errors"])

    rejected = bool(errors)
    reason = (
        "; ".join(errors)
        if rejected
        else "Required fields, email, and timestamp validated"
    )
    action = (
        "Rejected invalid request; data was not forwarded."
        if rejected
        else "Validated data is ready for the next processing stage."
    )

    if validator_result is not None and validator_result["valid"]:
        data_to_log = validator_result["cleaned_data"]
    else:
        data_to_log = payload.data

    # Persist the validation decision. A database failure must not be
    # reported as a successful durable log.
    _record_request(
        request_id=request_id,
        request_type="field_validation",
        event_type="validation_rejected" if rejected else "field_validation",
        source="shield.validate-request",
        active_server=None,
        shield_enabled=1,
        validation_status="rejected" if rejected else "passed",
        final_status="rejected" if rejected else "processed",
        processing_stage="Contain" if rejected else "Verify",
        payload=_safe_payload(data_to_log),
        recovery_action=action,
        error_message=reason if rejected else None,
        duration_ms=max(
            0, int((time.perf_counter() - started) * 1000)
        ),
    )

    if rejected:
        db = SessionLocal()
        try:
            db.add(
                Incident(
                    incident_type="validation_rejected",
                    affected_service="shield.validate-request",
                    severity="medium",
                    status="rejected",
                    description=f"Request {request_id} rejected: {reason}",
                    recovery_action=action,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to save validation incident")
        finally:
            db.close()

        raise HTTPException(
            status_code=400,
            detail={
                "decision": "reject",
                "stage": "validation",
                "reason": reason,
                "request_id": request_id,
            },
        )

    return {
        "decision": "allow",
        "stage": "validation",
        "reason": reason,
        "request_id": request_id,
        "validated_data": validator_result["cleaned_data"],
    }


@app.post("/api/shield/validate-age")
def validate_age(payload: AgeValidationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    result = shield.validate_age(payload.age)
    rejected = result["decision"] == "reject"

    action = (
        "Invalid age input rejected before processing."
        if rejected
        else "Age normalized and accepted."
    )

    _record_request(
        request_id=request_id,
        request_type="age_validation",
        event_type="age_validation",
        source="shield.validate-age",
        active_server=None,
        shield_enabled=1,
        validation_status="rejected" if rejected else "passed",
        final_status="rejected" if rejected else "processed",
        processing_stage="Contain" if rejected else "Verify",
        payload={"age": str(payload.age)[:80]},
        recovery_action=action,
        error_message=result.get("reason") if rejected else None,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )

    if rejected:
        raise HTTPException(
            status_code=400,
            detail={**result, "request_id": request_id},
        )

    return {**result, "request_id": request_id}


@app.post("/api/simulation/run")
def run_simulation(payload: SimulationPayload):
    """Run a simulated fault, validate requests, and log the outcome."""
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    event = payload.event_type
    source = payload.source

    action = "No failover required; request processed on the primary path."
    active_server: Optional[str] = "primary"
    validation_status = "passed"
    final_status = "processed"
    stage = "Verify"
    error_message = None
    data_to_log = _safe_payload(payload.data)

    try:
        if event == "validRequest":
            if payload.shield_enabled:
                # Required fields first, then email/timestamp validation.
                guard = RequiredFieldsGuard.validate(
                    payload.data,
                    ["name", "email"],
                )

                checked = (
                    validate_request_data(payload.data)
                    if guard["valid"]
                    else None
                )

                errors = []
                if not guard["valid"]:
                    errors.append(guard["error"])
                elif checked is not None and not checked["valid"]:
                    errors.extend(checked["errors"])

                if errors:
                    validation_status = "rejected"
                    final_status = "rejected"
                    stage = "Contain"
                    active_server = None
                    error_message = "; ".join(errors)
                    action = (
                        "Rejected invalid request; data was not forwarded "
                        "to the processing path."
                    )
                else:
                    data_to_log = _safe_payload(checked["cleaned_data"])
                    action = (
                        "Required fields, email, and timestamp passed "
                        "validation; request processed on the primary path."
                    )
            else:
                validation_status = "bypassed"
                final_status = "processed"
                action = (
                    "Demo request processed without validation because "
                    "the shield is disabled."
                )

        elif not payload.shield_enabled:
            validation_status = "bypassed"
            final_status = "failed"
            stage = "Detect"
            active_server = None
            action = (
                "Shield disabled; simulated fault was not contained "
                "and the dependent request failed."
            )
            error_message = f"Shield disabled during simulated event: {event}"

        elif event == "missingField":
            test_data = dict(payload.data)
            test_data.pop("email", None)
            checked = RequiredFieldsGuard.validate(
                test_data,
                ["name", "email"],
            )
            validation_status = "rejected" if not checked["valid"] else "passed"
            final_status = "rejected" if not checked["valid"] else "processed"
            stage = "Contain" if final_status == "rejected" else "Verify"
            active_server = None if final_status == "rejected" else "primary"
            action = (
                "Rejected incomplete payload; invalid data was not forwarded."
                if final_status == "rejected"
                else "Required fields passed validation."
            )
            error_message = checked.get("error") if final_status == "rejected" else None
            data_to_log = _safe_payload(test_data)

        elif event == "sourceDown":
            failed = source if source in ("primary", "backup") else "primary"
            fallback = "backup" if failed == "primary" else "primary"
            active_server = fallback
            final_status = "recovered"
            stage = "Verify"
            action = (
                f"Detected {failed} unavailable; switched to simulated "
                f"{fallback} and verified the fallback path."
            )

        elif event == "badTimestamp":
            validation_status = "rejected"
            final_status = "recovered"
            stage = "Recover"
            active_server = "backup"
            action = (
                "Contained invalid timestamp and used the simulated "
                "trusted backup path."
            )
            error_message = "Invalid or future timestamp detected in the simulation."

        elif event == "exception":
            final_status = "recovered"
            stage = "Recover"
            active_server = "backup"
            action = (
                "Caught the simulated application exception; routed "
                "processing through the backup path."
            )
            error_message = "Simulated application exception."

        elif event == "retryLoop":
            validation_status = "limited"
            final_status = "recovered"
            stage = "Recover"
            active_server = "backup"
            action = (
                "Stopped the simulated retry loop at the guard limit "
                "and selected the backup path."
            )
            error_message = "Simulated retry/loop limit reached."

        elapsed = max(0, int((time.perf_counter() - started) * 1000))

        _record_request(
            request_id=request_id,
            request_type="simulation",
            event_type=event,
            source=source,
            active_server=active_server,
            shield_enabled=1 if payload.shield_enabled else 0,
            validation_status=validation_status,
            final_status=final_status,
            processing_stage=stage,
            payload=data_to_log,
            recovery_action=action,
            error_message=error_message,
            duration_ms=elapsed,
        )

        if event != "validRequest" or final_status != "processed":
            _record_incident(event, source, final_status, action)

        return {
            "request_id": request_id,
            "event_type": event,
            "source": source,
            "active_server": active_server,
            "validation_status": validation_status,
            "final_status": final_status,
            "processing_stage": stage,
            "recovery_action": action,
            "error_message": error_message,
            "duration_ms": elapsed,
            "logged_to_postgresql": True,
        }

    except SQLAlchemyError as exc:
        logger.exception(
            "Database error while processing simulation request %s",
            request_id,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Request could not be logged to PostgreSQL; "
                "database connection failed."
            ),
        ) from exc

    except Exception as exc:
        logger.exception("Unexpected simulation error for %s", request_id)
        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request_id,
                "message": "Simulation failed; check backend logs.",
            },
        ) from exc


@app.get("/api/monitor/status")
def monitor_status():
    return {
        "monitor": get_monitor_state(),
        "check_interval_seconds": 5,
        "failures_before_unhealthy": 3,
    }


@app.post("/api/monitor/check")
def monitor_check_now():
    try:
        return {"results": check_services_once()}
    except Exception as exc:
        logger.exception("Manual health check failed")
        raise HTTPException(
            status_code=503,
            detail="Manual health check failed. Check backend logs.",
        ) from exc


@app.post("/api/monitor/simulate/{service}/{mode}")
def simulate_service(service: str, mode: str):
    try:
        return set_service_mode(service, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/requests")
def list_requests():
    db = SessionLocal()
    try:
        rows = (
            db.query(RequestLog)
            .order_by(desc(RequestLog.created_at), desc(RequestLog.id))
            .all()
        )
        return {
            "total": len(rows),
            "items": [
                {
                    "id": row.id,
                    "request_id": row.request_id,
                    "created_at": (
                        row.created_at.isoformat() if row.created_at else None
                    ),
                    "event_type": row.event_type,
                    "source": row.source,
                    "active_server": row.active_server,
                    "validation_status": row.validation_status,
                    "final_status": row.final_status,
                    "processing_stage": row.processing_stage,
                    "payload": row.payload,
                    "recovery_action": row.recovery_action,
                    "error_message": row.error_message,
                    "duration_ms": row.duration_ms,
                }
                for row in rows
            ],
        }
    finally:
        db.close()


@app.get("/api/overview")
def dashboard_overview():
    """Real overview metrics derived from PostgreSQL request and incident logs."""
    db = SessionLocal()
    try:
        requests = db.query(RequestLog).all()
        incidents = db.query(Incident).all()
        total = len(requests)
        recovered = sum(1 for r in requests if (r.final_status or "").lower() in ("recovered", "processed"))
        health_rows = db.execute(text("SELECT service_name, status, last_error, updated_at FROM service_health ORDER BY service_name")).mappings().all()
        healthy = sum(1 for r in health_rows if str(r["status"]).lower() in ("healthy", "ok", "up", "ready"))
        score = round(100 * healthy / len(health_rows)) if health_rows else None
        return {"health_score": score, "requests_processed": total,
                "active_incidents": sum(1 for i in incidents if (i.status or "").lower() not in ("resolved", "closed", "verified")),
                "failover_events": sum(1 for r in requests if (r.active_server or "").lower() == "backup"),
                "verification_success": round(100 * recovered / total, 1) if total else None,
                "servers": [dict(r) for r in health_rows]}
    finally:
        db.close()

@app.get("/api/servers")
def dashboard_servers():
    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT service_name, status, last_error, updated_at FROM service_health ORDER BY service_name")).mappings().all()
        return [{"id": r["service_name"], "name": r["service_name"], "role": "primary" if "primary" in r["service_name"].lower() or "a" == r["service_name"].lower() else "backup", "health": r["status"], "readiness": r["status"], "last_check_at": r["updated_at"].isoformat() if r["updated_at"] else None, "last_error": r["last_error"], "active": str(r["status"]).lower() in ("healthy", "ok", "up", "ready")} for r in rows]
    finally:
        db.close()

@app.get("/api/requests/stats")
def dashboard_request_stats(origin: str | None = None):
    db = SessionLocal()
    try:
        rows = db.query(RequestLog).all()
        if origin and origin != "all": rows = [r for r in rows if ("simulation" if r.request_type == "simulation" else "operational") == origin]
        status = lambda s: sum(1 for r in rows if (r.final_status or "").upper() == s)
        return {"total": len(rows), "success": status("PROCESSED"), "corrected": status("CORRECTED"), "rejected": status("REJECTED"), "pending": status("PENDING"), "failed": status("FAILED"), "recovered": status("RECOVERED")}
    finally: db.close()

@app.get("/api/incidents")
def dashboard_incidents():
    db = SessionLocal()
    try:
        rows = db.query(Incident).order_by(desc(Incident.created_at)).all()
        return [{"incident_id": f"INC-{r.id}", "id": r.id, "detected_at": r.created_at.isoformat() if r.created_at else None, "created_at": r.created_at.isoformat() if r.created_at else None, "title": r.incident_type, "incident_type": r.incident_type, "server": r.affected_service, "affected_service": r.affected_service, "severity": r.severity, "status": r.status, "description": r.description, "recovery_action": r.recovery_action, "recovery_result": r.recovery_action} for r in rows]
    finally: db.close()

@app.get("/api/logs")
def dashboard_logs(page_size: int = 50, page: int = 1):
    db = SessionLocal()
    try:
        rows = db.query(RequestLog).order_by(desc(RequestLog.created_at), desc(RequestLog.id)).all()
        data = [{"id": r.id, "timestamp": r.created_at.isoformat() if r.created_at else None, "created_at": r.created_at.isoformat() if r.created_at else None, "category": r.event_type, "severity": "warning" if (r.error_message or r.final_status in ("failed", "rejected")) else "info", "source": r.source, "request_id": r.request_id, "action": r.recovery_action, "outcome": r.final_status, "message": r.error_message or r.recovery_action} for r in rows]
        size = max(1, min(page_size, 500)); page = max(1, page)
        return {"data": data[(page-1)*size:page*size], "items": data[(page-1)*size:page*size], "meta": {"total": len(data), "all": len(data)}}
    finally: db.close()

@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    db = SessionLocal()
    try:
        row = (
            db.query(RequestLog)
            .filter(RequestLog.request_id == request_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")

        return {
            "request_id": row.request_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "event_type": row.event_type,
            "source": row.source,
            "active_server": row.active_server,
            "validation_status": row.validation_status,
            "final_status": row.final_status,
            "processing_stage": row.processing_stage,
            "payload": row.payload,
            "recovery_action": row.recovery_action,
            "error_message": row.error_message,
            "duration_ms": row.duration_ms,
        }
    finally:
        db.close()