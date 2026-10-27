from app.database import SessionLocal
from app.models import ShieldSettings


def get_settings() -> dict:
    db = SessionLocal()
    try:
        settings = db.query(ShieldSettings).filter_by(id=1).first()
        if settings is None:
            settings = ShieldSettings(id=1)
            db.add(settings)
            db.commit()
            db.refresh(settings)
        return {
            "shield_enabled": settings.shield_enabled,
            "monitoring_enabled": settings.monitoring_enabled,
            "failure_threshold": settings.failure_threshold,
            "overload_threshold": settings.overload_threshold,
            "active_server": settings.active_server,
            "primary_load_pct": settings.primary_load_pct,
            "backup_load_pct": settings.backup_load_pct,
            "server_a_capacity": settings.server_a_capacity,
            "server_b_capacity": settings.server_b_capacity,
            "server_a_utilization": settings.server_a_utilization,
            "server_b_utilization": settings.server_b_utilization,
            "recovery_threshold": settings.recovery_threshold,
            "admission_status": settings.admission_status,
            "capacity_exhausted": settings.capacity_exhausted,
            "form_admission_status": settings.form_admission_status,
            "simulation_active": settings.simulation_active,
            "accepted_requests": settings.accepted_requests,
            "rejected_requests": settings.rejected_requests,
            "queued_requests": settings.queued_requests,
            "completed_requests": settings.completed_requests,
            "capacity_exhaustion_incidents": settings.capacity_exhaustion_incidents,
            "last_capacity_event": settings.last_capacity_event,
            "updated_at": (
                settings.updated_at.isoformat() if settings.updated_at else None
            ),
        }
    finally:
        db.close()


def update_settings(**values) -> dict:
    allowed = {
        "shield_enabled",
        "monitoring_enabled",
        "failure_threshold",
        "overload_threshold",
        "active_server",
        "primary_load_pct",
        "backup_load_pct",
        "server_a_capacity",
        "server_b_capacity",
        "server_a_utilization",
        "server_b_utilization",
        "recovery_threshold",
        "admission_status",
        "capacity_exhausted",
        "form_admission_status",
        "simulation_active",
        "accepted_requests",
        "rejected_requests",
        "queued_requests",
        "completed_requests",
        "capacity_exhaustion_incidents",
        "last_capacity_event",
    }
    unknown = values.keys() - allowed
    if unknown:
        raise ValueError(f"Unsupported settings: {', '.join(sorted(unknown))}")

    db = SessionLocal()
    try:
        settings = db.query(ShieldSettings).filter_by(id=1).first()
        if settings is None:
            settings = ShieldSettings(id=1)
            db.add(settings)
        for key, value in values.items():
            setattr(settings, key, value)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return get_settings()


def is_monitoring_enabled() -> bool:
    try:
        settings = get_settings()
        return bool(settings["shield_enabled"] and settings["monitoring_enabled"])
    except Exception:
        return False
