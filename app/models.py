from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, JSON
from sqlalchemy.sql import func
from app.database import Base


class Incident(Base):
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True, index=True)
    incident_type = Column(String(100), nullable=False)
    affected_service = Column(String(100), nullable=False)
    severity = Column(String(20), nullable=False, default="medium")
    status = Column(String(30), nullable=False, default="detected")
    description = Column(Text, nullable=False)
    recovery_action = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ServiceHealth(Base):
    __tablename__ = "service_health"
    id = Column(Integer, primary_key=True, index=True)
    service_name = Column(String(100), nullable=False, unique=True)
    status = Column(String(30), nullable=False, default="healthy")
    last_error = Column(Text, nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HealthCheckLog(Base):
    __tablename__ = "health_check_logs"
    id = Column(Integer, primary_key=True, index=True)
    service_name = Column(String(100), nullable=False, index=True)
    result = Column(String(30), nullable=False)
    response_ms = Column(Integer, nullable=False, default=0)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    state = Column(String(30), nullable=False)
    check_type = Column(String(30), nullable=False, default="scheduled")
    checked_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class RequestLog(Base):
    """Durable audit record for each ChronoGuard simulation request."""

    __tablename__ = "request_logs"
    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(String(36), unique=True, index=True, nullable=False)
    request_type = Column(String(80), nullable=False)
    event_type = Column(String(80), nullable=False)
    source = Column(String(40), nullable=False)
    active_server = Column(String(40), nullable=True)
    shield_enabled = Column(Integer, nullable=False, default=1)
    validation_status = Column(String(30), nullable=False)
    final_status = Column(String(30), nullable=False)
    processing_stage = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=True)
    recovery_action = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ShieldSettings(Base):
    __tablename__ = "shield_settings"
    id = Column(Integer, primary_key=True)
    shield_enabled = Column(Boolean, nullable=False, default=True)
    monitoring_enabled = Column(Boolean, nullable=False, default=True)
    failure_threshold = Column(Integer, nullable=False, default=3)
    overload_threshold = Column(Integer, nullable=False, default=65)
    active_server = Column(String(20), nullable=False, default="primary")
    primary_load_pct = Column(Integer, nullable=False, default=0)
    backup_load_pct = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), unique=True, index=True, nullable=False)
    scenario_id = Column(String(80), nullable=False, index=True)
    scenario_name = Column(String(160), nullable=False)
    category = Column(String(80), nullable=False)
    status = Column(String(24), nullable=False, default="QUEUED")
    stage = Column(String(30), nullable=False, default="Monitor")
    progress = Column(Integer, nullable=False, default=0)
    shield_enabled = Column(Boolean, nullable=False, default=True)
    configuration = Column(JSON, nullable=False, default=dict)
    expected_result = Column(Text, nullable=False)
    actual_result = Column(Text, nullable=True)
    outcome = Column(String(24), nullable=False, default="RUNNING")
    metrics = Column(JSON, nullable=False, default=dict)
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
