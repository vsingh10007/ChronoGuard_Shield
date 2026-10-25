from datetime import datetime, timezone, timedelta


class TimestampGuard:
    def __init__(self, max_future_seconds=300):
        self.max_future_seconds = max_future_seconds

    def validate(self, timestamp, trusted_time):
        """
        timestamp: datetime or ISO-8601 timestamp string
        trusted_time: datetime from a trusted or simulated backup source
        """

        if trusted_time.tzinfo is None:
            raise ValueError("trusted_time must include a timezone")

        trusted_time = trusted_time.astimezone(timezone.utc)

        try:
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )

            if not isinstance(timestamp, datetime):
                raise ValueError("Timestamp must be a datetime or ISO string")

            if timestamp.tzinfo is None:
                raise ValueError("Timestamp must include a timezone")

            timestamp = timestamp.astimezone(timezone.utc)

            # Reject timestamps too far in the future.
            if timestamp > trusted_time + timedelta(
                seconds=self.max_future_seconds
            ):
                raise ValueError("Timestamp is too far in the future")

            return {
                "valid": True,
                "original_timestamp": timestamp.isoformat(),
                "effective_timestamp": timestamp.isoformat(),
                "recovered": False,
                "message": "Timestamp validated successfully",
            }

        except (ValueError, TypeError, OverflowError) as error:
            # Keep the original value for debugging and auditing.
            return {
                "valid": False,
                "original_timestamp": str(timestamp),
                "effective_timestamp": trusted_time.isoformat(),
                "recovered": True,
                "message": str(error),
            }