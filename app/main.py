import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import desc, text
from sqlalchemy.exc import SQLAlchemyError

from app.config_store import get_settings, update_settings
from app.database import Base, SessionLocal, engine
from app.models import HealthCheckLog, Incident, RequestLog, ServiceHealth
from app.monitor import (
    CHECK_INTERVAL_SECONDS,
    FAILURES_BEFORE_UNHEALTHY,
    TIMEOUT_SECONDS,
    check_services_once,
    get_monitor_state,
    set_service_mode,
    start_monitor,
    stop_monitor,
)
from app.request_validator import validate_request_data
from app.shield.required_fields_guard import RequiredFieldsGuard
from app.simulation_engine import (
    MAX_CONCURRENCY,
    MAX_REQUESTS,
    SCENARIOS,
    get_run,
    list_runs,
    reset_history,
    start_run,
    stop_run,
)

logger = logging.getLogger("chronoguard")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        columns = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'shield_settings'
                """
            )
        ).scalars().all()
        for definition in (
            ("server_a_capacity", "INTEGER NOT NULL DEFAULT 100"),
            ("server_b_capacity", "INTEGER NOT NULL DEFAULT 100"),
            ("server_a_utilization", "INTEGER NOT NULL DEFAULT 0"),
            ("server_b_utilization", "INTEGER NOT NULL DEFAULT 0"),
            ("recovery_threshold", "INTEGER NOT NULL DEFAULT 75"),
            ("admission_status", "VARCHAR(30) NOT NULL DEFAULT 'accepting_requests'"),
            ("capacity_exhausted", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("form_admission_status", "VARCHAR(20) NOT NULL DEFAULT 'accepting'"),
            ("simulation_active", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("accepted_requests", "INTEGER NOT NULL DEFAULT 0"),
            ("rejected_requests", "INTEGER NOT NULL DEFAULT 0"),
            ("queued_requests", "INTEGER NOT NULL DEFAULT 0"),
            ("completed_requests", "INTEGER NOT NULL DEFAULT 0"),
            ("capacity_exhaustion_incidents", "INTEGER NOT NULL DEFAULT 0"),
            ("last_capacity_event", "TEXT"),
        ):
            if definition[0] not in columns:
                conn.execute(
                    text(
                        f"ALTER TABLE shield_settings ADD COLUMN IF NOT EXISTS {definition[0]} {definition[1]}"
                    )
                )
    start_monitor()
    try:
        yield
    finally:
        stop_monitor()


app = FastAPI(
    title="ChronoGuard API",
    description="Self-Healing Dependency Guardian",
    version="1.2.0",
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


class RequestValidationPayload(BaseModel):
    data: dict[str, Any]
    required_fields: list[str] = Field(min_length=1)


class AgeValidationPayload(BaseModel):
    age: Any


class ShieldSettingsPayload(BaseModel):
    shield_enabled: bool | None = None
    monitoring_enabled: bool | None = None
    failure_threshold: int | None = Field(default=None, ge=1, le=10)
    overload_threshold: int | None = Field(default=None, ge=1, le=95)


class MonitorSettingsPayload(BaseModel):
    enabled: bool


class SimulationStartPayload(BaseModel):
    scenario_id: str
    config: dict[str, Any] = Field(default_factory=dict)


class LegacySimulationPayload(BaseModel):
    event_type: str = Field(
        pattern="^(validRequest|badTimestamp|sourceDown|missingField|exception|retryLoop)$"
    )
    source: str = Field(pattern="^(client|app|primary|backup)$")
    shield_enabled: bool = True
    data: dict[str, Any] = Field(default_factory=dict)


def _safe_payload(data: dict[str, Any]) -> dict[str, Any]:
    secret_names = ("password", "token", "secret", "authorization", "api_key")
    return {
        key: value
        for key, value in data.items()
        if not any(secret in key.lower() for secret in secret_names)
    }


def _record_request(**values) -> None:
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
                severity="high" if final_status in ("failed", "ERROR") else "medium",
                status=final_status,
                description=f"Simulation event '{event_type}' began at {source}.",
                recovery_action=action,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not persist ChronoGuard incident")
        raise
    finally:
        db.close()


def _origin(row: RequestLog) -> str:
    return "simulation" if "simulation" in (row.request_type or "") else "operational"


def _request_json(row: RequestLog) -> dict:
    payload = row.payload or {}
    error = row.error_message
    corrected = bool(payload.get("timestamp_correction", {}).get("corrected"))
    return {
        "id": row.id,
        "request_id": row.request_id,
        "request_type": row.request_type,
        "endpoint": row.source,
        "source": row.source,
        "received_at": row.created_at.isoformat() if row.created_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
        "original_payload": payload,
        "payload": payload,
        "validation_status": row.validation_status,
        "validation_errors": [error] if error else [],
        "detected_issue": error,
        "correction_applied": corrected,
        "correction_reason": payload.get("timestamp_correction", {}).get("reason"),
        "original_value": payload.get("timestamp_correction", {}).get(
            "original_timestamp"
        ),
        "corrected_value": payload.get("timestamp_correction", {}).get(
            "replacement_timestamp"
        ),
        "rejection_reason": (
            error if row.final_status in ("rejected", "failed") else None
        ),
        "final_status": (row.final_status or "unknown").upper(),
        "processing_status": row.final_status,
        "processing_server": row.active_server,
        "active_server": row.active_server,
        "processing_stage": row.processing_stage,
        "verification_status": (
            "VERIFIED" if row.final_status in ("processed", "recovered") else None
        ),
        "recovery_action": row.recovery_action,
        "duration_ms": row.duration_ms,
        "origin": _origin(row),
    }


def _incident_json(row: Incident) -> dict:
    return {
        "id": row.id,
        "incident_id": f"INC-{row.id}",
        "detected_at": row.created_at.isoformat() if row.created_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "title": row.incident_type,
        "incident_type": row.incident_type,
        "server": row.affected_service,
        "affected_service": row.affected_service,
        "severity": row.severity,
        "status": row.status,
        "description": row.description,
        "recovery_action": row.recovery_action,
        "recovery_result": row.recovery_action,
        "origin": (
            "simulation" if "simulation" in (row.incident_type or "") else "operational"
        ),
    }


def _monitor_summary(settings: dict) -> dict:
    snapshot = get_monitor_state()
    statuses = [item.get("status") for item in snapshot.values()]
    latest = max(
        (
            item.get("checked_at")
            for item in snapshot.values()
            if item.get("checked_at")
        ),
        default=None,
    )
    enabled = bool(settings["shield_enabled"] and settings["monitoring_enabled"])
    return {
        "enabled": enabled,
        "shield_enabled": settings["shield_enabled"],
        "monitoring_enabled": settings["monitoring_enabled"],
        "interval_seconds": CHECK_INTERVAL_SECONDS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "failure_threshold": settings["failure_threshold"] or FAILURES_BEFORE_UNHEALTHY,
        "last_check_at": latest,
        "last_check_type": "scheduled" if enabled else "paused",
        "services": snapshot,
        "status": (
            "unhealthy"
            if "unhealthy" in statuses
            else (
                "degraded"
                if "degraded" in statuses
                else (
                    "healthy"
                    if statuses and all(s == "healthy" for s in statuses)
                    else "unknown"
                )
            )
        ),
    }


_CAPACITY_LOCK = threading.Lock()


def _capacity_state_from_settings(settings: dict | None = None) -> dict:
    settings = settings or get_settings()
    server_a_capacity = max(1, int(settings.get("server_a_capacity", 100)))
    server_b_capacity = max(1, int(settings.get("server_b_capacity", 100)))
    server_a_utilization = max(0, min(100, int(settings.get("server_a_utilization", 0))))
    server_b_utilization = max(0, min(100, int(settings.get("server_b_utilization", 0))))
    recovery_threshold = max(50, min(95, int(settings.get("recovery_threshold", 75))))
    shield_enabled = bool(settings.get("shield_enabled", True))
    both_overloaded = server_a_utilization >= 100 and server_b_utilization >= 100
    admissions_open = shield_enabled and not both_overloaded
    server_a_available = max(0, int(round(server_a_capacity * (100 - server_a_utilization) / 100)))
    server_b_available = max(0, int(round(server_b_capacity * (100 - server_b_utilization) / 100)))
    if both_overloaded:
        global_status = "CAPACITY EXHAUSTED"
        form_admission_status = "closed"
        admission_status = "capacity_exhausted"
    elif not admissions_open:
        global_status = "WAITING FOR RECOVERY"
        form_admission_status = "closed"
        admission_status = "waiting_for_recovery"
    elif server_a_utilization >= recovery_threshold or server_b_utilization >= recovery_threshold:
        global_status = "RESUMING SERVICE"
        form_admission_status = "opening"
        admission_status = "resuming_service"
    else:
        global_status = "ACCEPTING REQUESTS"
        form_admission_status = "open"
        admission_status = "accepting_requests"
    return {
        "server_a_capacity": server_a_capacity,
        "server_b_capacity": server_b_capacity,
        "server_a_utilization": server_a_utilization,
        "server_b_utilization": server_b_utilization,
        "server_a_available_capacity": server_a_available,
        "server_b_available_capacity": server_b_available,
        "total_configured_capacity": server_a_capacity + server_b_capacity,
        "current_recovery_threshold": recovery_threshold,
        "recovery_threshold": recovery_threshold,
        "admission_open": admissions_open,
        "admission_status": admission_status,
        "form_admission_status": form_admission_status,
        "global_status": global_status,
        "capacity_exhausted": both_overloaded,
        "shield_enabled": shield_enabled,
        "currently_processing_requests": int(settings.get("accepted_requests", 0)) - int(settings.get("completed_requests", 0)),
        "pending_requests": int(settings.get("queued_requests", 0)),
        "completed_requests": int(settings.get("completed_requests", 0)),
        "accepted_requests": int(settings.get("accepted_requests", 0)),
        "rejected_requests": int(settings.get("rejected_requests", 0)),
        "capacity_exhaustion_incidents": int(settings.get("capacity_exhaustion_incidents", 0)),
        "simulation_active": bool(settings.get("simulation_active", False)),
        "server_a_state": "Full" if server_a_utilization >= 100 else "High Load" if server_a_utilization >= recovery_threshold else "Healthy",
        "server_b_state": "Full" if server_b_utilization >= 100 else "High Load" if server_b_utilization >= recovery_threshold else "Healthy",
    }


def _capacity_snapshot() -> dict:
    with _CAPACITY_LOCK:
        settings = get_settings()
        snapshot = _capacity_state_from_settings(settings)
        update_settings(
            admission_status=snapshot["admission_status"],
            capacity_exhausted=snapshot["capacity_exhausted"],
            form_admission_status=snapshot["form_admission_status"],
        )
        return snapshot


@app.get("/")
def root():
    return {
        "message": "ChronoGuard API is running",
        "status": "healthy",
        "version": "1.2.0",
    }


@app.get("/api/health")
def health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "api": "healthy",
            "database": "connected",
            "db": True,
        }
    except Exception as exc:
        logger.exception("Health check failed")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "api": "healthy",
                "database": "disconnected",
                "db": False,
            },
        ) from exc


@app.get("/api/shield/config")
def read_shield_config():
    try:
        return get_settings()
    except Exception as exc:
        logger.exception("Could not read Shield settings")
        raise HTTPException(
            status_code=503,
            detail="Shield settings are unavailable; initialize the database.",
        ) from exc


@app.put("/api/shield/config")
def write_shield_config(payload: ShieldSettingsPayload):
    values = payload.model_dump(exclude_none=True)
    if "shield_enabled" in values:
        values["monitoring_enabled"] = values["shield_enabled"]
    try:
        return update_settings(**values)
    except Exception as exc:
        logger.exception("Could not update Shield settings")
        raise HTTPException(
            status_code=503, detail="Shield settings could not be saved."
        ) from exc


@app.get("/api/monitor/status")
def monitor_status():
    try:
        return _monitor_summary(get_settings())
    except Exception as exc:
        logger.exception("Could not read monitor status")
        raise HTTPException(
            status_code=503, detail="Monitor configuration is unavailable."
        ) from exc


@app.put("/api/monitor/config")
def update_monitor_config(payload: MonitorSettingsPayload):
    try:
        settings = get_settings()
        if payload.enabled and not settings["shield_enabled"]:
            raise HTTPException(
                status_code=409,
                detail="Enable the ChronoGuard Health Shield before enabling automated checks.",
            )
        settings = update_settings(monitoring_enabled=payload.enabled)
        return _monitor_summary(settings)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not update monitor configuration")
        raise HTTPException(
            status_code=503, detail="Monitor configuration could not be saved."
        ) from exc


@app.post("/api/monitor/check")
def monitor_check_now():
    try:
        return {
            "results": check_services_once(check_type="manual"),
            "monitoring_enabled": _monitor_summary(get_settings())["enabled"],
        }
    except Exception as exc:
        logger.exception("Manual health check failed")
        raise HTTPException(
            status_code=503,
            detail="Manual health check failed; check database connectivity and backend logs.",
        ) from exc


@app.get("/api/capacity/status")
def capacity_status():
    try:
        return _capacity_snapshot()
    except Exception as exc:
        logger.exception("Could not read capacity status")
        raise HTTPException(status_code=503, detail="Capacity status unavailable.") from exc


@app.get("/api/capacity/history")
def capacity_history(limit: int = Query(default=25, ge=1, le=200)):
    settings = get_settings()
    history = []
    if settings.get("last_capacity_event"):
        history.append({
            "timestamp": settings["updated_at"],
            "status": settings["admission_status"],
            "message": settings["last_capacity_event"],
            "server_a_utilization": settings["server_a_utilization"],
            "server_b_utilization": settings["server_b_utilization"],
        })
    return {"data": history[:limit], "total": len(history), "limit": limit}


@app.get("/api/capacity/incidents")
def capacity_incidents():
    settings = get_settings()
    count = int(settings.get("capacity_exhaustion_incidents", 0))
    if count <= 0:
        return {"data": [], "total": 0}
    return {
        "data": [
            {
                "incident_id": "CAP-1",
                "status": settings.get("admission_status", "accepting_requests"),
                "severity": "high",
                "description": settings.get("last_capacity_event") or "Dual-server capacity exhaustion detected.",
                "detected_at": settings.get("updated_at"),
            }
        ],
        "total": 1,
    }


@app.post("/api/capacity/settings")
def update_capacity_settings(payload: dict[str, Any]):
    try:
        config = payload or {}
        values = {}
        for key in ("server_a_capacity", "server_b_capacity", "server_a_utilization", "server_b_utilization", "recovery_threshold"):
            if key in config:
                values[key] = int(config[key])
        if "shield_enabled" in config:
            values["shield_enabled"] = bool(config["shield_enabled"])
        if not values and not config:
            values = {"shield_enabled": True}
        settings = update_settings(**values)
        return _capacity_snapshot()
    except Exception as exc:
        logger.exception("Could not update capacity settings")
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/capacity/simulation/start")
def start_capacity_simulation(payload: dict[str, Any]):
    config = payload.get("config", payload) if isinstance(payload, dict) else {}
    try:
        args = {
            "server_a_capacity": int(config.get("server_a_capacity", config.get("server_a_max_capacity", 100))),
            "server_b_capacity": int(config.get("server_b_capacity", config.get("server_b_max_capacity", 100))),
            "server_a_utilization": int(config.get("server_a_initial_utilization", config.get("server_a_utilization", 0))),
            "server_b_utilization": int(config.get("server_b_initial_utilization", config.get("server_b_utilization", 0))),
            "recovery_threshold": int(config.get("recovery_threshold", 75)),
            "shield_enabled": bool(config.get("shield_enabled", True)),
            "simulation_active": True,
            "last_capacity_event": "Dual-server capacity simulation started.",
        }
        settings = update_settings(**args)
        snapshot = _capacity_snapshot()
        if snapshot["capacity_exhausted"]:
            settings = update_settings(
                capacity_exhaustion_incidents=int(settings.get("capacity_exhaustion_incidents", 0)) + 1,
                last_capacity_event="Capacity exhaustion detected; both servers reached configured maximum capacity.",
            )
            snapshot = _capacity_snapshot()
        return {"status": "started", **snapshot, "settings": settings}
    except Exception as exc:
        logger.exception("Could not start capacity simulation")
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/capacity/simulation/stop")
def stop_capacity_simulation():
    settings = update_settings(simulation_active=False, last_capacity_event="Dual-server capacity simulation stopped.")
    return {"status": "stopped", **_capacity_snapshot(), "settings": settings}


@app.post("/api/capacity/reset")
def reset_capacity_state():
    settings = update_settings(
        server_a_capacity=100,
        server_b_capacity=100,
        server_a_utilization=0,
        server_b_utilization=0,
        recovery_threshold=75,
        admission_status="accepting_requests",
        capacity_exhausted=False,
        form_admission_status="open",
        simulation_active=False,
        accepted_requests=0,
        rejected_requests=0,
        queued_requests=0,
        completed_requests=0,
        capacity_exhaustion_incidents=0,
        last_capacity_event="Capacity state reset.",
    )
    return {"status": "reset", **_capacity_snapshot(), "settings": settings}


@app.post("/api/capacity/form")
def submit_capacity_form(payload: dict[str, Any]):
    snapshot = _capacity_snapshot()
    if not snapshot["admission_open"]:
        body = payload or {}
        rejected_payload = {
            "full_name": body.get("full_name") or body.get("name") or "",
            "email": body.get("email") or "",
            "request_category": body.get("request_category") or body.get("category") or "general",
            "message": body.get("message") or "",
        }
        _record_request(
            request_id=str(uuid.uuid4()),
            request_type="capacity_form",
            event_type="capacity_exhaustion",
            source="form-page",
            active_server=None,
            shield_enabled=int(bool(get_settings().get("shield_enabled", True))),
            validation_status="rejected",
            final_status="rejected",
            processing_stage="Contain",
            payload=rejected_payload,
            recovery_action="Request rejected due to dual-server capacity exhaustion.",
            error_message="Service Temporarily Unavailable",
            duration_ms=0,
        )
        settings = get_settings()
        update_settings(
            rejected_requests=int(settings.get("rejected_requests", 0)) + 1,
            last_capacity_event="Rejected request because both servers were at maximum capacity.",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "title": "Service Temporarily Unavailable",
                "message": "Both servers are currently at maximum capacity. We cannot accept new requests at this moment. Please try again shortly.",
                "retry_after": 30,
                "server_a_utilization": snapshot["server_a_utilization"],
                "server_b_utilization": snapshot["server_b_utilization"],
                "status": snapshot["global_status"],
            },
            headers={"Retry-After": "30"},
        )
    body = payload or {}
    name = str(body.get("full_name") or body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    category = str(body.get("request_category") or body.get("category") or "general").strip()
    message = str(body.get("message") or "").strip()
    if not name or not email or not message:
        raise HTTPException(status_code=400, detail={"message": "Name, email, and message are required."})
    validated = validate_request_data({
        "name": name,
        "email": email,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    if not validated["valid"]:
        raise HTTPException(status_code=422, detail={"message": "; ".join(validated["errors"])})
    settings = get_settings()
    route = "Server A" if settings["server_a_utilization"] <= settings["server_b_utilization"] else "Server B"
    next_a = min(100, int(settings.get("server_a_utilization", 0)) + 10)
    next_b = min(100, int(settings.get("server_b_utilization", 0)) + 10)
    accepted_count = int(settings.get("accepted_requests", 0)) + 1
    completed_count = int(settings.get("completed_requests", 0)) + 1
    update_settings(
        accepted_requests=accepted_count,
        completed_requests=completed_count,
        server_a_utilization=next_a if route == "Server A" else settings.get("server_a_utilization", 0),
        server_b_utilization=next_b if route == "Server B" else settings.get("server_b_utilization", 0),
        last_capacity_event=f"Accepted request for {category} via {route}.",
    )
    _record_request(
        request_id=str(uuid.uuid4()),
        request_type="capacity_form",
        event_type="capacity_form",
        source="form-page",
        active_server="primary" if route == "Server A" else "backup",
        shield_enabled=int(bool(get_settings().get("shield_enabled", True))),
        validation_status="passed",
        final_status="processed",
        processing_stage="Verify",
        payload={
            "full_name": name,
            "email": email,
            "request_category": category,
            "message": message,
            "route": route,
        },
        recovery_action=f"Accepted request for {category} and routed to {route}.",
        duration_ms=0,
    )
    return {
        "status": "accepted",
        "route": route,
        "message": "Request accepted and routed to the selected server.",
        "capacity_status": _capacity_snapshot(),
    }


@app.post("/api/capacity/form/submit")
def submit_capacity_form_alias(payload: dict[str, Any]):
    return submit_capacity_form(payload)


@app.post("/api/monitor/simulate/{service}/{mode}")
def simulate_service(service: str, mode: str):
    try:
        return set_service_mode(service, mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/servers")
def dashboard_servers():
    try:
        settings = get_settings()
        db = SessionLocal()
        try:
            rows = db.query(ServiceHealth).all()
            health = {row.service_name.lower(): row for row in rows}
        finally:
            db.close()
        result = []
        for service, label in (("primary", "Server A"), ("backup", "Server B")):
            row = health.get(service)
            status = row.status if row else "unknown"
            load = settings[f"{service}_load_pct"]
            if load >= settings["overload_threshold"]:
                status = "overloaded"
            result.append(
                {
                    "id": service,
                    "name": label,
                    "service_name": service,
                    "role": service,
                    "health": status,
                    "status": status,
                    "readiness": status,
                    "last_check_at": (
                        row.updated_at.isoformat() if row and row.updated_at else None
                    ),
                    "last_error": row.last_error if row else None,
                    "active": settings["active_server"] == service,
                    "load_percent": load,
                    "load_kind": "bounded simulation concurrency; not host CPU utilization",
                }
            )
        return result
    except Exception as exc:
        logger.exception("Could not load server health")
        raise HTTPException(
            status_code=503, detail="Server health data is unavailable."
        ) from exc


@app.get("/api/servers/{service_name}/health-history")
def server_health_history(
    service_name: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    service = service_name.lower()
    if service not in ("primary", "backup"):
        raise HTTPException(status_code=404, detail="Server not found")
    db = SessionLocal()
    try:
        rows = (
            db.query(HealthCheckLog)
            .filter(HealthCheckLog.service_name == service)
            .order_by(desc(HealthCheckLog.checked_at), desc(HealthCheckLog.id))
            .limit(limit)
            .all()
        )
        return [
            {
                "service": row.service_name,
                "result": row.result,
                "status": row.state,
                "state": row.state,
                "response_ms": row.response_ms,
                "failure_count": row.consecutive_failures,
                "consecutive_failures": row.consecutive_failures,
                "error": row.error,
                "check_type": row.check_type,
                "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            }
            for row in rows
        ]
    finally:
        db.close()


@app.get("/api/overview")
def dashboard_overview():
    db = SessionLocal()
    try:
        requests = db.query(RequestLog).all()
        incidents = db.query(Incident).all()
        services = db.query(ServiceHealth).all()
        healthy = sum(
            1
            for row in services
            if (row.status or "").lower() in ("healthy", "ok", "up", "ready")
        )
        settings = get_settings()
        recovered = sum(
            1
            for row in requests
            if (row.final_status or "").lower() in ("recovered", "processed")
        )
        return {
            "health_score": round(100 * healthy / len(services)) if services else None,
            "requests_processed": len(requests),
            "active_incidents": sum(
                1
                for item in incidents
                if (item.status or "").lower() not in ("resolved", "closed", "verified")
            ),
            "failover_events": sum(
                1 for row in requests if (row.active_server or "").lower() == "backup"
            ),
            "latest_failover": (
                settings["active_server"]
                if settings["active_server"] == "backup"
                else None
            ),
            "verification_success": (
                round(100 * recovered / len(requests), 1) if requests else None
            ),
            "lifecycle": {
                "monitor": (
                    "active"
                    if settings["monitoring_enabled"] and settings["shield_enabled"]
                    else "paused"
                )
            },
        }
    finally:
        db.close()


@app.get("/api/requests/stats")
def dashboard_request_stats(origin: str | None = None):
    db = SessionLocal()
    try:
        rows = db.query(RequestLog).all()
        if origin and origin != "all":
            rows = [row for row in rows if _origin(row) == origin]
        statuses = [(row.final_status or "").lower() for row in rows]
        return {
            "total": len(rows),
            "success": sum(value == "processed" for value in statuses),
            "corrected": sum(value == "corrected" for value in statuses),
            "rejected": sum(value == "rejected" for value in statuses),
            "pending": sum(value == "pending" for value in statuses),
            "failed": sum(value == "failed" for value in statuses),
            "recovered": sum(value == "recovered" for value in statuses),
        }
    finally:
        db.close()


@app.get("/api/requests")
def list_requests(
    limit: int = Query(default=50, ge=1, le=500),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=500),
    status: str | None = None,
    origin: str | None = None,
    q: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    date_from: str | None = None,
    date_to: str | None = None,
):
    db = SessionLocal()
    try:
        rows = (
            db.query(RequestLog)
            .order_by(desc(RequestLog.created_at), desc(RequestLog.id))
            .all()
        )
        data = [_request_json(row) for row in rows]
        if origin and origin != "all":
            data = [item for item in data if item["origin"] == origin]
        if status:
            data = [item for item in data if item["final_status"] == status.upper()]
        if q:
            needle = q.lower()
            data = [
                item
                for item in data
                if needle
                in (item["request_id"] + str(item["detected_issue"] or "")).lower()
            ]
        if date_from:
            data = [
                item
                for item in data
                if item["received_at"] and item["received_at"][:10] >= date_from
            ]
        if date_to:
            data = [
                item
                for item in data
                if item["received_at"] and item["received_at"][:10] <= date_to
            ]
        allowed_sort = {
            "request_id",
            "created_at",
            "received_at",
            "final_status",
            "validation_status",
        }
        if sort in allowed_sort:
            data.sort(
                key=lambda item: str(item.get(sort) or ""),
                reverse=order.lower() != "asc",
            )
        total = len(data)
        size = page_size or limit
        start = (page - 1) * size
        return {
            "total": total,
            "items": data[start : start + size],
            "data": data[start : start + size],
            "meta": {"total": total},
        }
    finally:
        db.close()


@app.get("/api/requests/{request_id}/history")
def request_history(request_id: str):
    db = SessionLocal()
    try:
        row = db.query(RequestLog).filter_by(request_id=request_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        return [
            {
                "event_type": row.event_type,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "details": row.recovery_action or row.error_message,
            }
        ]
    finally:
        db.close()


@app.get("/api/requests/{request_id}")
def get_request(request_id: str):
    db = SessionLocal()
    try:
        row = db.query(RequestLog).filter_by(request_id=request_id).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Request not found")
        return _request_json(row)
    finally:
        db.close()


@app.get("/api/incidents")
def dashboard_incidents():
    db = SessionLocal()
    try:
        rows = db.query(Incident).order_by(desc(Incident.created_at)).all()
        return [_incident_json(row) for row in rows]
    finally:
        db.close()


@app.get("/api/logs")
def dashboard_logs(
    page_size: int = Query(default=50, ge=1, le=500),
    page: int = Query(default=1, ge=1),
    origin: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    q: str | None = None,
    request_id: str | None = None,
    incident_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    db = SessionLocal()
    try:
        request_rows = (
            db.query(RequestLog)
            .order_by(desc(RequestLog.created_at), desc(RequestLog.id))
            .all()
        )
        incident_rows = (
            db.query(Incident)
            .order_by(desc(Incident.created_at), desc(Incident.id))
            .all()
        )
        events = [
            {
                "id": row.id,
                "event_id": f"EV-{row.id}",
                "timestamp": row.created_at.isoformat() if row.created_at else None,
                "category": row.event_type,
                "severity": (
                    "warning"
                    if row.error_message or row.final_status in ("failed", "rejected")
                    else "info"
                ),
                "source": row.source,
                "server": row.active_server,
                "request_id": row.request_id,
                "action": row.recovery_action,
                "outcome": row.final_status,
                "message": row.error_message or row.recovery_action,
                "origin": _origin(row),
            }
            for row in request_rows
        ]
        events.extend(
            {
                "id": row.id,
                "event_id": f"INC-{row.id}",
                "timestamp": row.created_at.isoformat() if row.created_at else None,
                "category": "incident",
                "severity": row.severity,
                "source": row.affected_service,
                "server": row.affected_service,
                "incident_id": f"INC-{row.id}",
                "action": row.recovery_action,
                "outcome": row.status,
                "message": row.description,
                "origin": (
                    "simulation" if "simulation" in row.incident_type else "operational"
                ),
            }
            for row in incident_rows
        )
        events.sort(key=lambda item: item["timestamp"] or "", reverse=True)
        if origin and origin != "all":
            events = [item for item in events if item["origin"] == origin]
        if category:
            events = [item for item in events if item["category"] == category]
        if severity:
            events = [item for item in events if item["severity"] == severity]
        if request_id:
            events = [item for item in events if item.get("request_id") == request_id]
        if incident_id:
            events = [item for item in events if item.get("incident_id") == incident_id]
        if q:
            needle = q.lower()
            events = [item for item in events if needle in str(item).lower()]
        if date_from:
            events = [
                item
                for item in events
                if item["timestamp"] and item["timestamp"][:10] >= date_from
            ]
        if date_to:
            events = [
                item
                for item in events
                if item["timestamp"] and item["timestamp"][:10] <= date_to
            ]
        total = len(events)
        start = (page - 1) * page_size
        counts = {}
        for item in events:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        page_data = events[start : start + page_size]
        return {
            "data": page_data,
            "items": page_data,
            "meta": {
                "total": total,
                "all": len(request_rows) + len(incident_rows),
                "counts": counts,
            },
        }
    finally:
        db.close()


@app.post("/api/shield/validate-request")
def validate_request(payload: RequestValidationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    guard = RequiredFieldsGuard.validate(payload.data, payload.required_fields)
    validator = (
        validate_request_data(payload.data, tuple(payload.required_fields))
        if guard["valid"]
        else None
    )
    errors = [] if guard["valid"] else [guard["error"]]
    if validator and not validator["valid"]:
        errors.extend(validator["errors"])
    rejected = bool(errors)
    reason = (
        "; ".join(errors) if errors else "Required fields and request data validated."
    )
    action = (
        "Rejected invalid request; data was not forwarded."
        if rejected
        else "Validated data is ready for processing."
    )
    safe = _safe_payload(
        validator["cleaned_data"] if validator and validator["valid"] else payload.data
    )
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
        payload=safe,
        recovery_action=action,
        error_message=reason if rejected else None,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
    if rejected:
        _record_incident(
            "validation_rejected", "shield.validate-request", "rejected", action
        )
        raise HTTPException(
            status_code=400,
            detail={"decision": "reject", "reason": reason, "request_id": request_id},
        )
    return {
        "decision": "allow",
        "reason": reason,
        "request_id": request_id,
        "validated_data": validator["cleaned_data"],
    }


@app.post("/api/shield/validate-age")
def validate_age(payload: AgeValidationPayload):
    started = time.perf_counter()
    value = payload.age
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        result = {
            "decision": "reject",
            "stage": "input_validation",
            "reason": "Age must be an integer or supported number word.",
        }
    else:
        try:
            age = int(value.strip()) if isinstance(value, str) else value
            result = (
                {"decision": "allow", "stage": "input_validation", "age": age}
                if 0 <= age <= 130
                else {
                    "decision": "reject",
                    "stage": "input_validation",
                    "reason": "Age must be between 0 and 130.",
                }
            )
        except ValueError:
            result = {
                "decision": "reject",
                "stage": "input_validation",
                "reason": "Age must be numeric.",
            }
    rejected = result["decision"] == "reject"
    _record_request(
        request_id=str(uuid.uuid4()),
        request_type="age_validation",
        event_type="age_validation",
        source="shield.validate-age",
        active_server=None,
        shield_enabled=1,
        validation_status="rejected" if rejected else "passed",
        final_status="rejected" if rejected else "processed",
        processing_stage="Contain" if rejected else "Verify",
        payload={"age": str(value)[:80]},
        recovery_action="Invalid age rejected." if rejected else "Age accepted.",
        error_message=result.get("reason"),
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
    if rejected:
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/api/simulation/run")
def run_legacy_simulation(payload: LegacySimulationPayload):
    started = time.perf_counter()
    request_id = str(uuid.uuid4())
    settings = get_settings()
    shield_enabled = settings["shield_enabled"]
    event = payload.event_type
    status = "processed"
    validation = "bypassed" if not shield_enabled else "passed"
    active_server = settings["active_server"]
    stage = "Verify"
    error = None
    action = "Diagnostic simulation completed; no physical failover was performed."
    test_data = _safe_payload(payload.data)
    if event == "validRequest" and shield_enabled:
        checked = validate_request_data(payload.data)
        if not checked["valid"]:
            status = "rejected"
            validation = "rejected"
            stage = "Contain"
            active_server = None
            error = "; ".join(checked["errors"])
            action = "Invalid test request rejected by the production validator."
        else:
            test_data = _safe_payload(checked["cleaned_data"])
    elif event == "missingField" and shield_enabled:
        test_data.pop("email", None)
        checked = RequiredFieldsGuard.validate(test_data, ["name", "email"])
        status = "rejected" if not checked["valid"] else "processed"
        validation = "rejected" if not checked["valid"] else "passed"
        stage = "Contain" if status == "rejected" else "Verify"
        error = checked.get("error")
    elif event == "sourceDown" and shield_enabled:
        unavailable = (
            payload.source if payload.source in ("primary", "backup") else "primary"
        )
        active_server = "backup" if unavailable == "primary" else "primary"
        status = "recovered"
        stage = "Recover"
        action = (
            f"Simulated {unavailable} unavailable; selected simulated {active_server}."
        )
    elif event != "validRequest":
        status = "recovered" if shield_enabled else "failed"
        validation = "simulated" if shield_enabled else "bypassed"
        stage = "Recover" if shield_enabled else "Detect"
        active_server = "backup" if shield_enabled else None
        error = (
            None
            if shield_enabled
            else f"Shield disabled during diagnostic event: {event}"
        )
        action = (
            "Simulated event completed; result does not represent physical failover."
        )
    _record_request(
        request_id=request_id,
        request_type="simulation",
        event_type=event,
        source=payload.source,
        active_server=active_server,
        shield_enabled=int(shield_enabled),
        validation_status=validation,
        final_status=status,
        processing_stage=stage,
        payload=test_data,
        recovery_action=action,
        error_message=error,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
    )
    if event != "validRequest" or status != "processed":
        _record_incident(event, payload.source, status, action)
    return {
        "request_id": request_id,
        "event_type": event,
        "source": payload.source,
        "active_server": active_server,
        "validation_status": validation,
        "final_status": status,
        "processing_stage": stage,
        "recovery_action": action,
        "error_message": error,
        "logged_to_postgresql": True,
    }


@app.get("/api/simulations/scenarios")
def simulation_scenarios():
    return {
        "data": SCENARIOS,
        "total": len(SCENARIOS),
        "max_requests": MAX_REQUESTS,
        "max_concurrency": MAX_CONCURRENCY,
    }


@app.post("/api/simulations")
def start_simulation(payload: SimulationStartPayload):
    config = payload.config.copy()
    limits = {
        "request_count": (1, MAX_REQUESTS),
        "concurrency": (1, MAX_CONCURRENCY),
        "queue_capacity": (1, MAX_REQUESTS),
        "failure_threshold": (1, 10),
        "server_a_capacity": (1, 1000),
        "server_b_capacity": (1, 1000),
        "server_a_max_capacity": (1, 1000),
        "server_b_max_capacity": (1, 1000),
        "server_a_initial_utilization": (0, 100),
        "server_b_initial_utilization": (0, 100),
        "server_a_utilization": (0, 100),
        "server_b_utilization": (0, 100),
        "incoming_request_rate": (1, 1000),
        "processing_duration_ms": (1, 60000),
        "recovery_threshold": (50, 95),
        "simulation_duration_seconds": (1, 3600),
        "shield_enabled": (0, 1),
    }
    validated = {}
    for key, value in config.items():
        if key not in limits:
            raise HTTPException(
                status_code=422, detail=f"Unsupported simulation setting: {key}"
            )
        low, high = limits[key]
        if key == "shield_enabled":
            if not isinstance(value, bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"{key} must be a boolean",
                )
            validated[key] = value
            continue
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise HTTPException(
                status_code=422,
                detail=f"{key} must be an integer between {low} and {high}",
            )
        validated[key] = value
    if "failure_threshold" in validated:
        update_settings(failure_threshold=validated["failure_threshold"])
    try:
        return start_run(payload.scenario_id, validated)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        logger.exception("Could not start simulation")
        raise HTTPException(
            status_code=503, detail="Simulation could not be saved to PostgreSQL."
        ) from exc


@app.get("/api/simulations/history")
def simulation_history(limit: int = Query(default=50, ge=1, le=200)):
    items = list_runs(limit)
    return {"data": items, "items": items, "total": len(items)}


@app.get("/api/simulations")
def list_simulations(limit: int = Query(default=50, ge=1, le=200)):
    items = list_runs(limit)
    return {"data": items, "items": items, "total": len(items)}


@app.get("/api/simulations/{run_id}")
def simulation_result(run_id: str):
    result = get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Simulation run not found")
    return result


@app.post("/api/simulations/{run_id}/stop")
def stop_simulation(run_id: str):
    result = stop_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Simulation run not found")
    return result


@app.post("/api/simulations/reset")
def reset_simulations():
    try:
        deleted = reset_history()
        for service in ("primary", "backup"):
            set_service_mode(service, "healthy")
        settings = update_settings(
            active_server="primary", primary_load_pct=0, backup_load_pct=0
        )
        check_services_once()
        return {
            "deleted_simulation_runs": deleted,
            "request_logs_deleted": 0,
            "incidents_deleted": 0,
            "settings": settings,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Simulation reset failed")
        raise HTTPException(
            status_code=503, detail="Simulation state could not be reset."
        ) from exc


@app.get("/api/overview/health")
def overview_health():
    return {"monitor": _monitor_summary(get_settings()), "database": health_check()}
