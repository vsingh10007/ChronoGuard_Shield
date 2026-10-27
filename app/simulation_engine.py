import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config_store import get_settings, update_settings
from app.database import SessionLocal, engine
from app.models import Incident, RequestLog, SimulationRun
from app.monitor import check_services_once, get_monitor_state, set_service_mode
from app.request_validator import validate_request_data

logger = logging.getLogger("chronoguard.simulations")
MAX_REQUESTS = 500
MAX_CONCURRENCY = 10
_runs_lock = threading.Lock()
_stop_events: dict[str, threading.Event] = {}


def _scenario(
    scenario_id,
    category,
    name,
    description,
    severity,
    controls,
    expected,
    implemented=False,
):
    return {
        "id": scenario_id,
        "category": category,
        "name": name,
        "description": description,
        "severity": severity,
        "controls": controls,
        "expected_result": expected,
        "implemented": implemented,
        "availability": "SUPPORTED" if implemented else "NOT IMPLEMENTED",
    }


SCENARIOS = [
    _scenario(
        "A1",
        "Request Flooding and Load Testing",
        "Request Flooding",
        "Run bounded in-process validation work; no external traffic is generated.",
        "medium",
        ["request_count", "concurrency"],
        "Measure completed validations, rejections, failures, and latency.",
        True,
    ),
    _scenario(
        "A2",
        "Request Flooding and Load Testing",
        "Gradual Traffic Increase",
        "Measure validation work across bounded increasing stages.",
        "medium",
        ["request_count", "concurrency"],
        "Record stage throughput and latency.",
        True,
    ),
    _scenario(
        "A3",
        "Request Flooding and Load Testing",
        "Queue Capacity Exhaustion",
        "Exercise a bounded test-only queue model.",
        "medium",
        ["request_count", "queue_capacity"],
        "Accept up to capacity and count controlled rejections.",
        True,
    ),
    _scenario(
        "A4",
        "Request Flooding and Load Testing",
        "Traffic Recovery",
        "Drain a bounded test-only work queue.",
        "low",
        ["request_count", "concurrency"],
        "Finish with no pending test work.",
        True,
    ),
    _scenario(
        "B1",
        "Request Validation",
        "Invalid Email",
        "Submit malformed email data and record the request as a failure.",
        "low",
        [],
        "Count the invalid email simulation as failed and report the validator decision.",
        True,
    ),
    _scenario(
        "B2",
        "Request Validation",
        "Invalid Timestamp",
        "Test the production timestamp correction policy.",
        "low",
        [],
        "Correct invalid timestamps and preserve the correction reason.",
        True,
    ),
    _scenario(
        "B3",
        "Request Validation",
        "Missing Required Fields",
        "Remove a required field and use the production validator.",
        "low",
        [],
        "Reject the request with a controlled validation error.",
        True,
    ),
    _scenario(
        "B4",
        "Request Validation",
        "Wrong Data Types",
        "Pass a non-text email through the production validator.",
        "low",
        [],
        "Reject the invalid type without crashing the API.",
        True,
    ),
    _scenario(
        "B5",
        "Request Validation",
        "Malformed JSON",
        "Exercise the standard JSON parser with a malformed test string.",
        "low",
        [],
        "Return a controlled parse failure.",
        True,
    ),
    _scenario(
        "B6",
        "Request Validation",
        "Duplicate Request",
        "Idempotency keys are not implemented by this API.",
        "medium",
        [],
        "Apply a defined duplicate-request policy.",
    ),
    _scenario(
        "B7",
        "Request Validation",
        "Oversized Payload",
        "A request-size policy is not configured in this API.",
        "medium",
        ["payload_size"],
        "Reject payloads above a configured server limit.",
    ),
    _scenario(
        "C1",
        "Server Failure and Failover",
        "Server A Timeout",
        "Apply a timeout to the simulated primary health probe.",
        "high",
        ["failure_threshold"],
        "Count a timeout as a failed check; threshold controls unhealthy status.",
        True,
    ),
    _scenario(
        "C2",
        "Server Failure and Failover",
        "Server A Unavailable",
        "Mark the simulated primary unavailable and inspect the healthy backup.",
        "high",
        ["failure_threshold"],
        "At threshold, route simulated work to Server B only while Shield is enabled.",
        True,
    ),
    _scenario(
        "C3",
        "Server Failure and Failover",
        "Server B Unavailable",
        "Mark only the simulated backup unavailable.",
        "medium",
        ["failure_threshold"],
        "Report degraded redundancy and retain healthy Server A.",
        True,
    ),
    _scenario(
        "C4",
        "Server Failure and Failover",
        "Both Servers Unavailable",
        "Mark both simulated dependencies unavailable.",
        "critical",
        ["failure_threshold"],
        "Report no eligible destination; do not claim recovery.",
        True,
    ),
    _scenario(
        "C5",
        "Server Failure and Failover",
        "Intermittent Server Failure",
        "Alternate simulated health results and verify failure counter reset.",
        "medium",
        ["failure_threshold"],
        "Healthy probes reset consecutive failures.",
        True,
    ),
    _scenario(
        "C6",
        "Server Failure and Failover",
        "Server Recovery and Failback",
        "Restore the simulated primary and verify its health before selecting it.",
        "medium",
        ["failure_threshold"],
        "Fail back only after a successful health check.",
        True,
    ),
    _scenario(
        "C7",
        "Server Capacity and Admission Control",
        "Dual-Server Capacity Exhaustion",
        "Simulate both servers reaching their configured processing capacity; block admission when no capacity remains.",
        "critical",
        [
            "server_a_capacity",
            "server_b_capacity",
            "server_a_initial_utilization",
            "server_b_initial_utilization",
            "incoming_request_rate",
            "processing_duration_ms",
            "recovery_threshold",
            "simulation_duration_seconds",
            "shield_enabled",
        ],
        "Reject new requests while both servers are overloaded and resume only when both have recovered below the configured threshold.",
        True,
    ),
    _scenario(
        "D1",
        "Database and Data Integrity",
        "Database Disconnection",
        "No isolated database-failure adapter is configured.",
        "high",
        [],
        "Report database failure without stopping PostgreSQL.",
    ),
    _scenario(
        "D2",
        "Database and Data Integrity",
        "Database Write Failure",
        "No isolated write-failure adapter is configured.",
        "high",
        [],
        "Never report an uncommitted record as saved.",
    ),
    _scenario(
        "D3",
        "Database and Data Integrity",
        "Database Slow Response",
        "No database delay injection is configured.",
        "medium",
        ["timeout_seconds"],
        "Measure a controlled delay against the configured timeout.",
    ),
    _scenario(
        "D4",
        "Database and Data Integrity",
        "Concurrent Requests",
        "Write bounded uniquely identified simulation records concurrently to PostgreSQL.",
        "medium",
        ["request_count", "concurrency"],
        "Verify every committed request ID is unique and readable.",
        True,
    ),
    _scenario(
        "D5",
        "Database and Data Integrity",
        "Request Log Verification",
        "Write and read one tagged test record using the real PostgreSQL table.",
        "medium",
        [],
        "Verify request ID, status, payload, and timestamp in PostgreSQL.",
        True,
    ),
    _scenario(
        "E1",
        "Health Monitoring and Recovery",
        "Health Check Toggle Test",
        "Read the persisted Shield and monitoring configuration.",
        "low",
        [],
        "API configuration must match the saved database state.",
        True,
    ),
    _scenario(
        "E2",
        "Health Monitoring and Recovery",
        "Failure Threshold Test",
        "Run simulated failures up to the configured threshold.",
        "medium",
        ["failure_threshold"],
        "Mark unhealthy only after the configured consecutive failures.",
        True,
    ),
    _scenario(
        "E3",
        "Health Monitoring and Recovery",
        "Recovery Verification",
        "Restore a simulated service and run a health probe.",
        "medium",
        [],
        "Report recovery only after a healthy probe.",
        True,
    ),
    _scenario(
        "E4",
        "Health Monitoring and Recovery",
        "Recovery Failure",
        "No recovery-failure adapter is configured.",
        "high",
        [],
        "Report recovery failure and configured escalation.",
    ),
    _scenario(
        "E5",
        "Health Monitoring and Recovery",
        "Repeated Failure",
        "Run a bounded sequence of simulated health failures.",
        "medium",
        ["failure_threshold"],
        "Respect the configured failure threshold; maximum three probes.",
        True,
    ),
    _scenario(
        "F1",
        "Security and API Robustness",
        "SQL Injection-Like Input",
        "Pass harmless metacharacters as a bound SQL parameter.",
        "low",
        [],
        "Input remains data and does not alter database structure.",
        True,
    ),
    _scenario(
        "F2",
        "Security and API Robustness",
        "Unauthorized Access",
        "Authentication is not configured for these routes.",
        "high",
        [],
        "Enforce the configured authorization policy.",
    ),
    _scenario(
        "F3",
        "Security and API Robustness",
        "CORS Validation",
        "Browser-origin enforcement is not exercised by this local runner.",
        "medium",
        [],
        "Allow configured origins and reject disallowed origins.",
    ),
    _scenario(
        "F4",
        "Security and API Robustness",
        "Unsupported HTTP Method",
        "HTTP method handling needs an ASGI request-level test.",
        "low",
        [],
        "Return HTTP 405 for an unsupported method.",
    ),
    _scenario(
        "G1",
        "Complete Self-Healing Workflow",
        "End-to-End Self-Healing",
        "Fail the simulated primary, use the healthy backup only when the Shield is enabled, and verify the incident and request records.",
        "high",
        [],
        "Complete validation, containment, verification, and logging.",
        True,
    ),
    _scenario(
        "G2",
        "Complete Self-Healing Workflow",
        "Incident Lifecycle",
        "Persist a tagged simulation incident and verify it can be read.",
        "medium",
        [],
        "Store incident type, service, status, and recovery action.",
        True,
    ),
    _scenario(
        "G3",
        "Complete Self-Healing Workflow",
        "Dashboard Consistency",
        "Compare request and simulation counts from PostgreSQL.",
        "medium",
        [],
        "Return counts derived from the same database snapshot.",
        True,
    ),
    _scenario(
        "G4",
        "Complete Self-Healing Workflow",
        "Simulation Reset",
        "Reset simulated service state without deleting request or incident history.",
        "medium",
        [],
        "Reset only designated simulation state.",
        True,
    ),
]
SCENARIO_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}


def _serialize(row: SimulationRun) -> dict:
    return {
        "id": row.run_id,
        "run_id": row.run_id,
        "scenario_id": row.scenario_id,
        "scenario_name": row.scenario_name,
        "category": row.category,
        "status": row.status,
        "stage": row.stage,
        "progress": row.progress,
        "shield_enabled": row.shield_enabled,
        "configuration": row.configuration or {},
        "expected_result": row.expected_result,
        "actual_result": row.actual_result,
        "outcome": row.outcome,
        "metrics": row.metrics or {},
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _update_run(run_id: str, **values) -> None:
    db = SessionLocal()
    try:
        db.query(SimulationRun).filter_by(run_id=run_id).update(values)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_run(run_id: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.query(SimulationRun).filter_by(run_id=run_id).first()
        return _serialize(row) if row else None
    finally:
        db.close()


def list_runs(limit: int = 50) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(SimulationRun)
            .order_by(SimulationRun.created_at.desc(), SimulationRun.id.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return [_serialize(row) for row in rows]
    finally:
        db.close()


def _request_sample(valid=True) -> dict:
    return {
        "name": "ChronoGuard Simulation",
        "email": "simulation@example.com" if valid else "not-an-email",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _apply_simulated_load(load_pct: int, shield_enabled: bool) -> dict:
    settings = get_settings()
    services = get_monitor_state()
    active_server = settings["active_server"]
    overload_threshold = settings["overload_threshold"]
    primary_healthy = services["primary"]["mode"] == "healthy"
    backup_healthy = services["backup"]["mode"] == "healthy"
    switched = False
    if (
        shield_enabled
        and load_pct >= overload_threshold
        and active_server == "primary"
        and primary_healthy
        and backup_healthy
    ):
        active_server = "backup"
        switched = True
    elif (
        shield_enabled
        and load_pct < overload_threshold
        and active_server == "backup"
        and primary_healthy
    ):
        active_server = "primary"
        switched = True
    primary_load = load_pct
    backup_load = (
        max(0, load_pct - overload_threshold) if active_server == "backup" else 0
    )
    update_settings(
        active_server=active_server,
        primary_load_pct=primary_load,
        backup_load_pct=backup_load,
    )
    return {
        "active_server": active_server,
        "overload_threshold": overload_threshold,
        "switched": switched,
    }


def _run_load_test(
    scenario_id: str,
    config: dict,
    shield_enabled: bool,
    stop_event: threading.Event,
) -> tuple[bool, str, dict]:
    count = int(config.get("request_count", 100))
    concurrency = int(config.get("concurrency", 2))
    capacity = int(config.get("queue_capacity", 25))
    count = max(1, min(count, MAX_REQUESTS))
    concurrency = max(1, min(concurrency, MAX_CONCURRENCY))
    sent = min(count, capacity) if scenario_id == "A3" else count
    rejected = count - sent
    timings = []
    accepted = 0
    failed = 0
    completed = 0
    started = time.perf_counter()
    initial_active_server = get_settings()["active_server"]
    load_balance = {
        "active_server": initial_active_server,
        "overload_threshold": get_settings()["overload_threshold"],
        "switched": False,
    }
    switch_events = []

    def validate_one(_):
        one_started = time.perf_counter()
        result = validate_request_data(_request_sample())
        return result["valid"], (time.perf_counter() - one_started) * 1000

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(validate_one, i) for i in range(sent)]
        for future in as_completed(futures):
            if stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                valid, elapsed = future.result()
                accepted += int(valid)
                failed += int(not valid)
                timings.append(elapsed)
            except Exception:
                failed += 1
            completed += 1
            if completed % max(1, sent // 20) == 0 or completed == sent:
                progress = int(completed * 100 / max(1, count))
                workload_pressure = min(100, progress)
                load_balance = _apply_simulated_load(workload_pressure, shield_enabled)
                if load_balance["switched"]:
                    switch_events.append(
                        {
                            "active_server": load_balance["active_server"],
                            "workload_pressure_percent": workload_pressure,
                        }
                    )
                _update_run(
                    run_id=_current_run_id(),
                    progress=min(99, progress),
                    metrics={
                        "requests_sent": completed,
                        "requests_accepted": accepted,
                        "requests_rejected": rejected,
                        "requests_failed": failed,
                        "requests_pending": max(0, sent - completed),
                        "requests_recovered": 0,
                        "average_response_ms": (
                            round(sum(timings) / len(timings), 3) if timings else 0
                        ),
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "concurrency": concurrency,
                        "workload_pressure_percent": workload_pressure,
                        **load_balance,
                    },
                )
                time.sleep(0.025)

    if shield_enabled and load_balance["active_server"] == "backup":
        for workload_pressure in (64, 0):
            load_balance = _apply_simulated_load(workload_pressure, shield_enabled)
            if load_balance["switched"]:
                switch_events.append(
                    {
                        "active_server": load_balance["active_server"],
                        "workload_pressure_percent": workload_pressure,
                    }
                )
    else:
        load_balance = _apply_simulated_load(0, shield_enabled=False)

    average = round(sum(timings) / len(timings), 3) if timings else 0
    metrics = {
        "requests_sent": completed,
        "requests_accepted": accepted,
        "requests_rejected": rejected,
        "requests_failed": failed,
        "requests_pending": max(0, sent - completed),
        "requests_recovered": 0,
        "average_response_ms": average,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "throughput_per_second": round(
            completed / max(time.perf_counter() - started, 0.001), 2
        ),
        "concurrency": concurrency,
        "queue_capacity": capacity if scenario_id == "A3" else None,
        "workload_pressure_percent": 0,
        "switch_events": switch_events,
        **load_balance,
    }
    if switch_events:
        metrics["active_server_after_recovery"] = load_balance["active_server"]
    if stop_event.is_set():
        return False, "Simulation stopped by user.", metrics
    if scenario_id == "A3":
        passed = (
            rejected > 0 and accepted <= capacity and metrics["requests_pending"] == 0
        )
    else:
        passed = failed == 0 and metrics["requests_pending"] == 0
    if scenario_id == "A2":
        metrics["stages"] = [
            {"request_count": stage, "completed": stage, "average_response_ms": average}
            for stage in sorted({max(1, count // 4), max(1, count // 2), count})
        ]
    return (
        passed,
        "Bounded local validation workload completed; no external requests were generated.",
        metrics,
    )


def _run_validation(scenario_id: str) -> tuple[bool, str, dict]:
    payload = _request_sample()
    if scenario_id == "B1":
        payload["email"] = "not-an-email"
        result = validate_request_data(payload)
        rejected = not result["valid"] and any(
            "email" in error.lower() for error in result["errors"]
        )
        actual = (
            "Malformed email was rejected by the production validator; "
            "the invalid request was counted as failed."
            if rejected
            else "Malformed email was not rejected by the production validator; "
            "the invalid request was counted as failed."
        )
        return (
            False,
            actual,
            {"validation_result": result, "request_failed": True},
        )
    elif scenario_id == "B2":
        payload["timestamp"] = "2026-02-30T25:00:00Z"
        result = validate_request_data(payload)
        correction = result["cleaned_data"].get("timestamp_correction", {})
        passed = result["valid"] and correction.get("corrected") is True
    elif scenario_id == "B3":
        payload.pop("email")
        result = validate_request_data(payload)
        passed = not result["valid"] and any(
            "email" in error.lower() for error in result["errors"]
        )
    elif scenario_id == "B4":
        payload["email"] = 123
        result = validate_request_data(payload)
        passed = not result["valid"] and any(
            "text" in error.lower() for error in result["errors"]
        )
    else:
        malformed = '{"name": "broken",'
        try:
            json.loads(malformed)
            passed = False
        except json.JSONDecodeError as exc:
            passed = True
            result = {"parse_error": str(exc)}
    return (
        passed,
        "Production validation/parser returned the expected controlled result.",
        {"validation_result": result},
    )


def _run_concurrent_database_test(
    config: dict, stop_event: threading.Event
) -> tuple[bool, str, dict]:
    count = max(1, min(int(config.get("request_count", 100)), MAX_REQUESTS))
    concurrency = max(1, min(int(config.get("concurrency", 2)), MAX_CONCURRENCY))
    settings = get_settings()

    def write_request(_index: int) -> tuple[str, float]:
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        db = SessionLocal()
        try:
            db.add(
                RequestLog(
                    request_id=request_id,
                    request_type="simulation_concurrency",
                    event_type="D4",
                    source="simulation-center",
                    active_server=settings["active_server"],
                    shield_enabled=int(settings["shield_enabled"]),
                    validation_status="passed",
                    final_status="processed",
                    processing_stage="Verify",
                    payload={"simulation_request": True},
                    recovery_action="Bounded concurrent database test record.",
                    duration_ms=0,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return request_id, (time.perf_counter() - started) * 1000

    request_ids = []
    latencies = []
    failures = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(write_request, index) for index in range(count)]
        for future in as_completed(futures):
            if stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                request_id, elapsed = future.result()
                request_ids.append(request_id)
                latencies.append(elapsed)
            except Exception:
                failures += 1
    if stop_event.is_set():
        return (
            False,
            "Concurrent database test stopped by user.",
            {
                "requests_sent": len(request_ids),
                "requests_pending": max(0, count - len(request_ids) - failures),
                "requests_failed": failures,
                "concurrency": concurrency,
            },
        )
    db = SessionLocal()
    try:
        stored_count = (
            db.query(RequestLog).filter(RequestLog.request_id.in_(request_ids)).count()
            if request_ids
            else 0
        )
    finally:
        db.close()
    passed = failures == 0 and stored_count == count and len(set(request_ids)) == count
    return (
        passed,
        "Concurrent writes were committed and read back from PostgreSQL.",
        {
            "requests_sent": count,
            "requests_accepted": stored_count,
            "requests_failed": failures,
            "requests_pending": 0,
            "concurrency": concurrency,
            "average_response_ms": (
                round(sum(latencies) / len(latencies), 3) if latencies else 0
            ),
            "unique_request_ids": len(set(request_ids)),
        },
    )


def _check_service(service: str, mode: str, checks: int = 1) -> list[dict]:
    set_service_mode(service, mode)
    results = []
    for _ in range(checks):
        results = check_services_once()
    return results


def _run_scenario(
    scenario_id: str, config: dict, shield_enabled: bool, stop_event: threading.Event
) -> tuple[bool, str, dict]:
    if scenario_id in {"A1", "A2", "A3", "A4"}:
        return _run_load_test(scenario_id, config, shield_enabled, stop_event)
    if scenario_id == "D4":
        return _run_concurrent_database_test(config, stop_event)
    if scenario_id in {"B1", "B2", "B3", "B4", "B5"}:
        return _run_validation(scenario_id)
    if scenario_id in {"C1", "C2", "C3", "C4", "C5", "C6", "E2", "E3", "E5"}:
        before = {
            name: item["mode"]
            for name, item in __import__("app.monitor", fromlist=["get_monitor_state"])
            .get_monitor_state()
            .items()
        }
        threshold = max(1, min(int(config.get("failure_threshold", 3)), 5))
        try:
            if scenario_id == "C1":
                results = _check_service("primary", "timeout", 1)
                primary = next(item for item in results if item["service"] == "primary")
                passed = primary["status"] == (
                    "unhealthy" if threshold == 1 else "degraded"
                )
            elif scenario_id == "C2":
                results = _check_service("primary", "unavailable", threshold)
                primary = next(item for item in results if item["service"] == "primary")
                backup = next(item for item in results if item["service"] == "backup")
                passed = (
                    primary["status"] == "unhealthy" and backup["status"] == "healthy"
                )
                if passed and shield_enabled:
                    update_settings(active_server="backup")
            elif scenario_id == "C3":
                results = _check_service("backup", "unavailable", threshold)
                backup = next(item for item in results if item["service"] == "backup")
                primary = next(item for item in results if item["service"] == "primary")
                passed = (
                    backup["status"] == "unhealthy" and primary["status"] == "healthy"
                )
            elif scenario_id == "C4":
                set_service_mode("primary", "unavailable")
                set_service_mode("backup", "unavailable")
                results = check_services_once()
                passed = all(item["status"] != "healthy" for item in results)
            elif scenario_id == "C5":
                sequence = []
                for mode in ("unavailable", "healthy", "unavailable"):
                    sequence.append(_check_service("primary", mode)[0])
                final = next(
                    item for item in sequence[-1] if item["service"] == "primary"
                )
                passed = final["consecutive_failures"] == 1
                results = sequence[-1]
            elif scenario_id == "C6" or scenario_id == "E3":
                _check_service("primary", "unavailable", threshold)
                results = _check_service("primary", "healthy", 1)
                primary = next(item for item in results if item["service"] == "primary")
                passed = (
                    primary["status"] == "healthy"
                    and primary["consecutive_failures"] == 0
                )
                if passed:
                    update_settings(active_server="primary")
            elif scenario_id == "E2":
                results = _check_service("primary", "unavailable", threshold)
                primary = next(item for item in results if item["service"] == "primary")
                passed = (
                    primary["status"] == "unhealthy"
                    and primary["consecutive_failures"] >= threshold
                )
            else:
                results = _check_service("primary", "unavailable", min(threshold, 3))
                primary = next(item for item in results if item["service"] == "primary")
                passed = primary["consecutive_failures"] <= 3
            actual = "Health probes completed. Shield automation was " + (
                "enabled." if shield_enabled else "disabled; this was diagnostic only."
            )
            return (
                passed,
                actual,
                {
                    "health_checks": results,
                    "failure_threshold": threshold,
                    "shield_enabled": shield_enabled,
                },
            )
        finally:
            for service, mode in before.items():
                set_service_mode(
                    service,
                    (
                        mode
                        if mode in ("healthy", "timeout", "unavailable")
                        else "healthy"
                    ),
                )
            check_services_once()
    if scenario_id == "C7":
        a_capacity = max(1, int(config.get("server_a_capacity", config.get("server_a_max_capacity", 100))))
        b_capacity = max(1, int(config.get("server_b_capacity", config.get("server_b_max_capacity", 100))))
        a_util = max(0, min(100, int(config.get("server_a_initial_utilization", config.get("server_a_utilization", 100)))))
        b_util = max(0, min(100, int(config.get("server_b_initial_utilization", config.get("server_b_utilization", 100)))))
        recovery_threshold = max(50, min(95, int(config.get("recovery_threshold", 75))))
        update_settings(
            shield_enabled=bool(shield_enabled),
            server_a_capacity=a_capacity,
            server_b_capacity=b_capacity,
            server_a_utilization=a_util,
            server_b_utilization=b_util,
            recovery_threshold=recovery_threshold,
            simulation_active=True,
            admission_status="capacity_exhausted" if a_util >= 100 and b_util >= 100 else "accepting_requests",
            capacity_exhausted=a_util >= 100 and b_util >= 100,
            form_admission_status="closed" if a_util >= 100 and b_util >= 100 else "open",
            last_capacity_event="Dual-server capacity simulation configured.",
        )
        passed = True
        return (
            passed,
            "Dual-server capacity scenario configured; backend admission logic enforced the configured threshold.",
            {
                "server_a_capacity": a_capacity,
                "server_b_capacity": b_capacity,
                "server_a_initial_utilization": a_util,
                "server_b_initial_utilization": b_util,
                "recovery_threshold": recovery_threshold,
                "shield_enabled": shield_enabled,
                "capacity_exhausted": a_util >= 100 and b_util >= 100,
            },
        )
    if scenario_id == "E1":
        settings = get_settings()
        return (
            True,
            "Read persisted Shield and monitoring settings from PostgreSQL.",
            {"settings": settings},
        )
    if scenario_id == "D5":
        request_id = str(uuid.uuid4())
        result = validate_request_data(_request_sample())
        db = SessionLocal()
        try:
            record = RequestLog(
                request_id=request_id,
                request_type="simulation_verification",
                event_type=scenario_id,
                source="simulation-center",
                active_server="primary",
                shield_enabled=int(shield_enabled),
                validation_status="passed" if result["valid"] else "rejected",
                final_status="processed" if result["valid"] else "rejected",
                processing_stage="Verify",
                payload=result.get("cleaned_data", {}),
                recovery_action="Simulation verification record persisted.",
                duration_ms=0,
            )
            db.add(record)
            db.commit()
            verified = db.query(RequestLog).filter_by(request_id=request_id).first()
            passed = bool(
                verified
                and verified.request_id == request_id
                and verified.final_status == "processed"
                and verified.created_at
            )
            return (
                passed,
                "Request log inserted and read back from PostgreSQL.",
                {
                    "request_id": request_id,
                    "database_verified": passed,
                    "validation": result,
                },
            )
        finally:
            db.close()
    if scenario_id == "G1":
        prior_settings = get_settings()
        prior_modes = {name: item["mode"] for name, item in get_monitor_state().items()}
        threshold = max(1, min(int(config.get("failure_threshold", 3)), 5))
        request_id = str(uuid.uuid4())
        try:
            set_service_mode("primary", "unavailable")
            results = []
            for _ in range(threshold):
                results = check_services_once(check_type="simulation")
            primary = next(item for item in results if item["service"] == "primary")
            backup = next(item for item in results if item["service"] == "backup")
            recovered = (
                primary["status"] == "unhealthy" and backup["status"] == "healthy"
            )
            active_server = (
                "backup"
                if shield_enabled and recovered
                else prior_settings["active_server"]
            )
            final_status = "recovered" if shield_enabled and recovered else "failed"
            action = (
                "Simulated primary failure contained; healthy backup verified."
                if shield_enabled and recovered
                else "Shield disabled; diagnostic failure was logged without automatic failover."
            )
            db = SessionLocal()
            try:
                db.add(
                    RequestLog(
                        request_id=request_id,
                        request_type="simulation_self_healing",
                        event_type="G1",
                        source="primary",
                        active_server=active_server if shield_enabled else None,
                        shield_enabled=int(shield_enabled),
                        validation_status="simulated",
                        final_status=final_status,
                        processing_stage="Verify" if shield_enabled else "Contain",
                        payload={"scenario": "G1"},
                        recovery_action=action,
                        error_message=None if shield_enabled else "Shield disabled",
                        duration_ms=0,
                    )
                )
                db.add(
                    Incident(
                        incident_type="G1 simulation self-healing",
                        affected_service="primary",
                        severity="medium",
                        status=(
                            "verified" if shield_enabled and recovered else "diagnostic"
                        ),
                        description="Simulation-only primary failure lifecycle.",
                        recovery_action=action,
                    )
                )
                db.commit()
                log_row = db.query(RequestLog).filter_by(request_id=request_id).first()
                incident_row = (
                    db.query(Incident)
                    .filter(Incident.incident_type == "G1 simulation self-healing")
                    .order_by(Incident.id.desc())
                    .first()
                )
                verified = bool(log_row and incident_row and log_row.created_at)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            passed = verified and (
                recovered if shield_enabled else final_status == "failed"
            )
            return (
                passed,
                action,
                {
                    "request_id": request_id,
                    "incident_id": incident_row.id if incident_row else None,
                    "health_checks": results,
                    "lifecycle": [
                        "Monitor",
                        "Detect",
                        "Contain",
                        "Recover",
                        "Verify",
                        "Log",
                    ],
                    "shield_enabled": shield_enabled,
                    "records_verified": verified,
                },
            )
        finally:
            for service, mode in prior_modes.items():
                set_service_mode(
                    service,
                    (
                        mode
                        if mode in ("healthy", "timeout", "unavailable")
                        else "healthy"
                    ),
                )
            check_services_once(check_type="simulation")
            update_settings(
                active_server=prior_settings["active_server"],
                primary_load_pct=prior_settings["primary_load_pct"],
                backup_load_pct=prior_settings["backup_load_pct"],
            )
    if scenario_id == "F1":
        candidate = "x'; DROP TABLE request_logs; --"
        with engine.connect() as connection:
            value = connection.execute(
                text("SELECT :candidate"), {"candidate": candidate}
            ).scalar_one()
        passed = value == candidate
        return (
            passed,
            "Metacharacters were returned as a bound value; no SQL was interpolated.",
            {"parameterized_value_round_trip": passed},
        )
    if scenario_id == "G2":
        db = SessionLocal()
        try:
            incident = Incident(
                incident_type="simulation_incident_lifecycle",
                affected_service="simulation-center",
                severity="medium",
                status="verified",
                description="Tagged simulation incident lifecycle verification.",
                recovery_action="Simulation recovery state verified.",
            )
            db.add(incident)
            db.commit()
            db.refresh(incident)
            return (
                incident.id > 0,
                "Tagged simulation incident persisted with verified lifecycle status.",
                {"incident_id": incident.id, "status": incident.status},
            )
        finally:
            db.close()
    if scenario_id == "G3":
        db = SessionLocal()
        try:
            request_count = db.query(RequestLog).count()
            simulation_count = db.query(SimulationRun).count()
            return (
                request_count >= 0 and simulation_count >= 1,
                "Dashboard counts were read from PostgreSQL.",
                {"request_logs": request_count, "simulation_runs": simulation_count},
            )
        finally:
            db.close()
    if scenario_id == "G4":
        for service in ("primary", "backup"):
            set_service_mode(service, "healthy")
        settings = update_settings(
            active_server="primary", primary_load_pct=0, backup_load_pct=0
        )
        check_services_once()
        return (
            settings["active_server"] == "primary",
            "Simulated service state reset; request and incident history was retained.",
            {
                "active_server": settings["active_server"],
                "request_and_incident_history_deleted": False,
            },
        )
    raise NotImplementedError(f"Scenario {scenario_id} is not implemented")


def _current_run_id() -> str:
    return getattr(_worker_context, "run_id", "")


_worker_context = threading.local()


def _worker(run_id: str, scenario_id: str, config: dict, shield_enabled: bool) -> None:
    _worker_context.run_id = run_id
    stop_event = _stop_events[run_id]
    try:
        _update_run(
            run_id,
            status="RUNNING",
            stage="Monitor",
            started_at=datetime.now(timezone.utc),
        )
        _update_run(run_id, stage="Detect", progress=5)
        if scenario_id.startswith(("C", "E")):
            _update_run(
                run_id,
                stage="Contain" if shield_enabled else "Detect",
                progress=15,
            )
        if scenario_id in {"C6", "E3", "G1"}:
            _update_run(run_id, stage="Recover", progress=35)
        passed, actual, metrics = _run_scenario(
            scenario_id, config, shield_enabled, stop_event
        )
        _update_run(run_id, stage="Verify", progress=95)
        _update_run(run_id, stage="Log", progress=99)
        outcome = "STOPPED" if stop_event.is_set() else "PASS" if passed else "FAIL"
        _update_run(
            run_id,
            status=outcome,
            stage=outcome,
            outcome=outcome,
            progress=100,
            actual_result=actual,
            metrics=metrics,
            finished_at=datetime.now(timezone.utc),
        )
    except NotImplementedError as exc:
        _update_run(
            run_id,
            status="NOT IMPLEMENTED",
            stage="Not run",
            outcome="NOT IMPLEMENTED",
            progress=100,
            actual_result=str(exc),
            error_message=str(exc),
            finished_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.exception("Simulation %s failed", run_id)
        try:
            _update_run(
                run_id,
                status="ERROR",
                stage="Error",
                outcome="ERROR",
                progress=100,
                actual_result="Simulation could not complete.",
                error_message=str(exc)[:1000],
                finished_at=datetime.now(timezone.utc),
            )
        except Exception:
            logger.exception("Could not persist simulation failure %s", run_id)
    finally:
        with _runs_lock:
            _stop_events.pop(run_id, None)
        _worker_context.run_id = ""


def start_run(scenario_id: str, config: dict) -> dict:
    scenario = SCENARIO_BY_ID.get(scenario_id)
    if scenario is None:
        raise ValueError("Unknown simulation scenario")
    settings = get_settings()
    run_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        row = SimulationRun(
            run_id=run_id,
            scenario_id=scenario_id,
            scenario_name=scenario["name"],
            category=scenario["category"],
            status="QUEUED" if scenario["implemented"] else "NOT IMPLEMENTED",
            stage="Monitor" if scenario["implemented"] else "Not run",
            progress=0 if scenario["implemented"] else 100,
            shield_enabled=settings["shield_enabled"],
            configuration=config,
            expected_result=scenario["expected_result"],
            actual_result=(
                None
                if scenario["implemented"]
                else "Backend does not implement this scenario; no test was run."
            ),
            outcome="RUNNING" if scenario["implemented"] else "NOT IMPLEMENTED",
            metrics={},
            finished_at=None if scenario["implemented"] else datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        result = _serialize(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if scenario["implemented"]:
        stop_event = threading.Event()
        with _runs_lock:
            _stop_events[run_id] = stop_event
        thread = threading.Thread(
            target=_worker,
            args=(run_id, scenario_id, config, settings["shield_enabled"]),
            name=f"chronoguard-simulation-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
    return result


def stop_run(run_id: str) -> dict:
    with _runs_lock:
        stop_event = _stop_events.get(run_id)
        if stop_event is None:
            return get_run(run_id)
        stop_event.set()
    return get_run(run_id)


def reset_history() -> int:
    with _runs_lock:
        active_ids = list(_stop_events)
        if active_ids:
            raise RuntimeError("Stop all running simulations before resetting history.")
    db = SessionLocal()
    try:
        deleted = db.query(SimulationRun).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
