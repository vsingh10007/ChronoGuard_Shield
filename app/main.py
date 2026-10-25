import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text, desc
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine, SessionLocal, Base
from app.models import Incident, RequestLog
from app.shield.controller import ShieldController

logger = logging.getLogger("chronoguard")
app = FastAPI(title="ChronoGuard API", description="Self-Healing Dependency Guardian", version="1.1.0")
# Supports local frontend testing via VS Code Live Server and localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500", "http://127.0.0.1:5501", "http://localhost:5501", "null"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
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
    data: dict[str, Any] = Field(default_factory=lambda: {"name": "Demo User", "email": "demo@example.com"})


def _record_request(**values) -> None:
    """Persist a request independently; failures are logged rather than hidden."""
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


def _record_incident(event_type: str, source: str, final_status: str, action: str) -> None:
    db = SessionLocal()
    try:
        db.add(Incident(
            incident_type=event_type,
            affected_service=source,
            severity="high" if final_status == "failed" else "medium",
            status=final_status,
            description=f"Simulation event '{event_type}' began at {source}.",
            recovery_action=action,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not persist ChronoGuard incident")
    finally:
        db.close()


@app.get("/")
def root():
    return {"message": "ChronoGuard API is running", "status": "healthy", "version": "1.1.0"}


@app.get("/api/health")
def health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"api": "healthy", "database": "connected"}
    except Exception as exc:
        logger.exception("Health check failed")
        raise HTTPException(status_code=503, detail={"api": "running", "database": "disconnected"}) from exc


@app.post("/api/shield/validate-request")
def validate_request(payload: RequestValidationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    result = shield.validate_request(payload.data, payload.required_fields)
    rejected = result["decision"] == "reject"
    action = "Incomplete data was not forwarded." if rejected else "Required fields passed validation."
    safe_payload = {k: v for k, v in payload.data.items() if not any(s in k.lower() for s in ("password", "token", "secret", "authorization", "api_key"))}
    _record_request(
        request_id=request_id, request_type="field_validation", event_type="missing_required_fields" if rejected else "field_validation",
        source="shield.validate-request", active_server=None, shield_enabled=1,
        validation_status="rejected" if rejected else "passed", final_status="rejected" if rejected else "processed",
        processing_stage="Contain" if rejected else "Verify", payload=safe_payload, recovery_action=action,
        error_message=result.get("reason") if rejected else None,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
    if rejected:
        db = SessionLocal()
        try:
            db.add(Incident(
                incident_type="missing_required_fields", affected_service="shield.validate-request",
                severity="medium", status="rejected",
                description=f"Request rejected; missing fields: {result.get('missing_fields', [])}",
                recovery_action=action,
            ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Failed to save validation incident")
        finally:
            db.close()
        raise HTTPException(status_code=400, detail={**result, "request_id": request_id})
    return {**result, "request_id": request_id}


@app.post("/api/shield/validate-age")
def validate_age(payload: AgeValidationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    result = shield.validate_age(payload.age)
    rejected = result["decision"] == "reject"
    action = "Invalid age input rejected before processing." if rejected else "Age normalized and accepted."
    _record_request(
        request_id=request_id, request_type="age_validation", event_type="age_validation",
        source="shield.validate-age", active_server=None, shield_enabled=1,
        validation_status="rejected" if rejected else "passed", final_status="rejected" if rejected else "processed",
        processing_stage="Contain" if rejected else "Verify", payload={"age": str(payload.age)[:80]},
        recovery_action=action, error_message=result.get("reason") if rejected else None,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
    if rejected:
        raise HTTPException(status_code=400, detail={**result, "request_id": request_id})
    return {**result, "request_id": request_id}


@app.post("/api/simulation/run")
def run_simulation(payload: SimulationPayload):
    """Run one deterministic simulated fault and persist its outcome in PostgreSQL."""
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

    
    try:
        if event == "validRequest":
            if payload.shield_enabled:
                checked = shield.validate_request(payload.data, ["name", "email"])

                if checked["decision"] == "reject":
                    validation_status = "rejected"
                    final_status = "rejected"
                    stage = "Contain"
                    action = "Rejected incomplete request; invalid data was not forwarded."
                    error_message = checked.get("reason")
                else:
                    validation_status = "passed"
                    final_status = "processed"
                    stage = "Verify"
                    active_server = "primary"
                    action = "Required fields passed validation; valid request processed on the primary path."
            else:
                validation_status = "bypassed"
                final_status = "processed"
                stage = "Verify"
                active_server = "primary"
                action = (
                    "Valid demo payload processed on the primary path without "
                    "ChronoGuard validation because the shield is disabled."
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
            data = dict(payload.data)
            data.pop("email", None)
            checked = shield.validate_request(data, ["name", "email"])
            validation_status = (
                "rejected" if checked["decision"] == "reject" else "passed"
            )
            final_status = (
                "rejected" if validation_status == "rejected" else "processed"
            )
            stage = "Contain" if final_status == "rejected" else "Verify"
            action = (
                "Rejected incomplete payload; invalid data was not forwarded."
                if final_status == "rejected"
                else "Required fields passed validation."
            )
            error_message = (
                checked.get("reason") if final_status == "rejected" else None
            )

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
                "Caught the simulated application exception; "
                "routed processing through the backup path."
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

        # Store only demo payload fields; never credentials or secrets.
        safe_payload = {
            k: v
            for k, v in payload.data.items()
            if not any(
                s in k.lower()
                for s in (
                    "password",
                    "token",
                    "secret",
                    "authorization",
                    "api_key",
                )
            )
        }

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
            payload=safe_payload,
            recovery_action=action,
            error_message=error_message,
            duration_ms=elapsed,
        )

        # Normal requests are logged, but are not incidents.
        if event != "validRequest":
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
        logger.exception(
            "Unexpected simulation error for request %s",
            request_id,
        )
        elapsed = max(0, int((time.perf_counter() - started) * 1000))

        # Best-effort durable record for unexpected failures.
        try:
            _record_request(
                request_id=request_id,
                request_type="simulation",
                event_type=event,
                source=source,
                active_server=None,
                shield_enabled=1 if payload.shield_enabled else 0,
                validation_status="error",
                final_status="failed",
                processing_stage="Detect",
                payload=payload.data,
                recovery_action=(
                    "No recovery action completed; inspect backend logs."
                ),
                error_message=str(exc)[:1000],
                duration_ms=elapsed,
            )
        except Exception:
            logger.exception(
                "Unable to persist failure record for request %s",
                request_id,
            )

        raise HTTPException(
            status_code=500,
            detail={
                "request_id": request_id,
                "message": "Simulation failed; check backend logs.",
            },
        ) from exc



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
                    "id": r.id,
                    "request_id": r.request_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "event_type": r.event_type,
                    "source": r.source,
                    "active_server": r.active_server,
                    "validation_status": r.validation_status,
                    "final_status": r.final_status,
                    "processing_stage": r.processing_stage,
                    "payload": r.payload,
                    "recovery_action": r.recovery_action,
                    "error_message": r.error_message,
                    "duration_ms": r.duration_ms,
                }
                for r in rows
            ],
        }
    finally:
        db.close()



@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    db = SessionLocal()
    try:
        row = db.query(RequestLog).filter(RequestLog.request_id == request_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        return {"request_id": row.request_id, "created_at": row.created_at.isoformat() if row.created_at else None,
                "event_type": row.event_type, "source": row.source, "active_server": row.active_server,
                "validation_status": row.validation_status, "final_status": row.final_status,
                "processing_stage": row.processing_stage, "payload": row.payload,
                "recovery_action": row.recovery_action, "error_message": row.error_message, "duration_ms": row.duration_ms}
    finally:
        db.close()
