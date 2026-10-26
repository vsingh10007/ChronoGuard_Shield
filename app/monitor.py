
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import engine

logger = logging.getLogger("chronoguard.monitor")

CHECK_INTERVAL_SECONDS = 5
TIMEOUT_SECONDS = 2
FAILURES_BEFORE_UNHEALTHY = 3

# Protect shared state and prevent overlapping health checks.
_state_lock = threading.Lock()
_check_lock = threading.Lock()
_stop_event = threading.Event()
_monitor_thread = None

# Simulated service controls for safe failure testing.
_services = {
    "primary": {
        "mode": "healthy",
        "failures": 0,
        "status": "unknown",
        "persisted_status": None,
    },
    "backup": {
        "mode": "healthy",
        "failures": 0,
        "status": "unknown",
        "persisted_status": None,
    },
}


def set_service_mode(service: str, mode: str) -> dict:
    """Set a simulated service mode for testing."""
    if service not in _services:
        raise ValueError("Service must be primary or backup.")

    if mode not in ("healthy", "timeout", "unavailable"):
        raise ValueError(
            "Mode must be healthy, timeout, or unavailable."
        )

    with _state_lock:
        _services[service]["mode"] = mode

    return {"service": service, "mode": mode}


def get_monitor_state() -> dict:
    """Return a snapshot of the current simulated service state."""
    with _state_lock:
        return {
            name: {
                key: value
                for key, value in state.items()
                if key != "persisted_status"
            }
            for name, state in _services.items()
        }


def _probe_service(service: str) -> tuple[str, int, str | None]:
    """Probe a simulated service and return status, latency, and error."""
    started = time.perf_counter()

    with _state_lock:
        mode = _services[service]["mode"]

    if mode == "timeout":
        time.sleep(TIMEOUT_SECONDS + 0.2)
        return (
            "timeout",
            int((time.perf_counter() - started) * 1000),
            "Simulated health-check timeout.",
        )

    if mode == "unavailable":
        return (
            "unavailable",
            int((time.perf_counter() - started) * 1000),
            "Simulated service unavailable.",
        )

    return (
        "healthy",
        int((time.perf_counter() - started) * 1000),
        None,
    )


def _save_health_change(
    service: str,
    status: str,
    error_message: str | None,
) -> None:
    """
    Save the latest health status for a service.

    The existing schema has a unique constraint on service_name,
    so update the existing row instead of inserting duplicates.
    """
    updated_at = datetime.now(timezone.utc)

    with engine.begin() as connection:
        result = connection.execute(
            text(
                """
                UPDATE service_health
                SET
                    status = :status,
                    last_error = :last_error,
                    updated_at = :updated_at
                WHERE service_name = :service_name
                """
            ),
            {
                "service_name": service,
                "status": status,
                "last_error": error_message,
                "updated_at": updated_at,
            },
        )

        # Create the row only if this service has never been recorded.
        if result.rowcount == 0:
            connection.execute(
                text(
                    """
                    INSERT INTO service_health
                        (service_name, status, last_error, updated_at)
                    VALUES
                        (:service_name, :status, :last_error, :updated_at)
                    """
                ),
                {
                    "service_name": service,
                    "status": status,
                    "last_error": error_message,
                    "updated_at": updated_at,
                },
            )


def check_services_once() -> list[dict]:
    """Check both services and persist status transitions."""
    with _check_lock:
        results = []

        for service in ("primary", "backup"):
            probe_status, latency_ms, probe_error = _probe_service(
                service
            )

            with _state_lock:
                state = _services[service]

                if probe_status == "healthy":
                    state["failures"] = 0
                    new_status = "healthy"
                else:
                    state["failures"] += 1
                    new_status = (
                        "unhealthy"
                        if state["failures"] >= FAILURES_BEFORE_UNHEALTHY
                        else "degraded"
                    )

                state["status"] = new_status
                failures = state["failures"]
                mode = state["mode"]
                persisted_status = state["persisted_status"]

            # Save a new status, or retry a previous failed database write.
            if new_status != persisted_status:
                try:
                    _save_health_change(
                        service=service,
                        status=new_status,
                        error_message=probe_error,
                    )
                except SQLAlchemyError:
                    logger.exception(
                        "Could not save health status for %s; "
                        "will retry on the next check.",
                        service,
                    )
                else:
                    with _state_lock:
                        _services[service]["persisted_status"] = (
                            new_status
                        )

            result = {
                "service": service,
                "mode": mode,
                "status": new_status,
                "consecutive_failures": failures,
                "response_time_ms": latency_ms,
                "error": probe_error,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

            results.append(result)
            logger.info("Health check: %s", result)

        return results


def _monitor_loop() -> None:
    """Run health checks continuously until stopped."""
    logger.info("ChronoGuard background monitor started.")

    while not _stop_event.is_set():
        try:
            check_services_once()
        except Exception:
            logger.exception("Unexpected error in monitoring loop")

        _stop_event.wait(CHECK_INTERVAL_SECONDS)

    logger.info("ChronoGuard background monitor stopped.")


def start_monitor() -> None:
    """Start the background monitoring thread if not already running."""
    global _monitor_thread

    if _monitor_thread and _monitor_thread.is_alive():
        return

    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        name="chronoguard-monitor",
        daemon=True,
    )
    _monitor_thread.start()


def stop_monitor() -> None:
    """Request the background monitoring thread to stop."""
    _stop_event.set()

    if (
        _monitor_thread
        and _monitor_thread.is_alive()
        and threading.current_thread() is not _monitor_thread
    ):
        _monitor_thread.join(timeout=CHECK_INTERVAL_SECONDS + TIMEOUT_SECONDS + 1)
