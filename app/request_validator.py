
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from typing import Any


# ChronoGuard validation limits.
MAX_FUTURE_SKEW_SECONDS = 30
MAX_TIMESTAMP_AGE_HOURS = 24


def validate_request_data(
    data: dict[str, Any],
    required_fields: tuple[str, ...] = ("name", "email"),
) -> dict[str, Any]:
    """
    Validate request fields and an optional ISO-8601 timestamp.

    Required fields must exist and contain non-empty text.
    Email addresses must have a reasonable email format.
    Invalid timestamps are replaced with the current UTC timestamp
    and the correction is recorded.

    Returns:
        {
            "valid": bool,
            "errors": list[str],
            "cleaned_data": dict
        }
    """

    errors: list[str] = []

    # Work on a copy so the caller's original payload remains unchanged.
    if not isinstance(data, dict):
        return {
            "valid": False,
            "errors": ["Request data must be a JSON object."],
            "cleaned_data": {},
        }

    cleaned_data = dict(data)

    # 1. Validate required fields.
    for field in required_fields:
        value = cleaned_data.get(field)

        if field not in cleaned_data or value is None:
            errors.append(f"Required field '{field}' is missing.")

        elif not isinstance(value, str):
            errors.append(f"Field '{field}' must be text.")

        elif not value.strip():
            errors.append(f"Required field '{field}' cannot be empty.")

        else:
            cleaned_data[field] = value.strip()

    # 2. Validate email format.
    email = cleaned_data.get("email")

    if isinstance(email, str) and email.strip():
        email_value = email.strip()
        _, parsed_email = parseaddr(email_value)

        if (
            not parsed_email
            or parsed_email != email_value
            or any(character.isspace() for character in email_value)
            or email_value.count("@") != 1
        ):
            errors.append("Email address format is invalid.")

        else:
            local_part, domain = email_value.rsplit("@", 1)

            if (
                not local_part
                or not domain
                or "." not in domain
                or domain.startswith(".")
                or domain.endswith(".")
                or ".." in domain
            ):
                errors.append("Email address format is invalid.")
            else:
                cleaned_data["email"] = email_value

    # 3. Validate and normalize the timestamp.
    now = datetime.now(timezone.utc)
    replacement_timestamp = (
        now.isoformat().replace("+00:00", "Z")
    )

    original_timestamp = cleaned_data.get("timestamp")
    corrected = False
    correction_reason: str | None = None
    parsed_timestamp: datetime | None = None

    if original_timestamp is None:
        corrected = True
        correction_reason = "Timestamp was missing."

    elif (
        not isinstance(original_timestamp, str)
        or not original_timestamp.strip()
    ):
        corrected = True
        correction_reason = "Timestamp was empty or not text."

    else:
        timestamp_text = original_timestamp.strip()

        # Accept ISO-8601 UTC timestamps ending in Z or z.
        if timestamp_text.endswith(("Z", "z")):
            timestamp_text = timestamp_text[:-1] + "+00:00"

        try:
            parsed_timestamp = datetime.fromisoformat(timestamp_text)

            if (
                parsed_timestamp.tzinfo is None
                or parsed_timestamp.utcoffset() is None
            ):
                corrected = True
                correction_reason = "Timestamp had no timezone."

            else:
                timestamp_utc = parsed_timestamp.astimezone(
                    timezone.utc
                )

                if timestamp_utc > now + timedelta(
                    seconds=MAX_FUTURE_SKEW_SECONDS
                ):
                    corrected = True
                    correction_reason = (
                        "Timestamp was too far in the future."
                    )

                elif timestamp_utc < now - timedelta(
                    hours=MAX_TIMESTAMP_AGE_HOURS
                ):
                    corrected = True
                    correction_reason = "Timestamp was too old."

        except (ValueError, OverflowError):
            corrected = True
            correction_reason = "Timestamp format was invalid."

    if corrected:
        cleaned_data["timestamp"] = replacement_timestamp
        cleaned_data["timestamp_correction"] = {
            "corrected": True,
            "reason": correction_reason,
            "original_timestamp": original_timestamp,
            "replacement_timestamp": replacement_timestamp,
        }

    else:
        # Normalize valid timestamps to UTC.
        assert parsed_timestamp is not None

        cleaned_data["timestamp"] = (
            parsed_timestamp.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        # Avoid retaining an old correction record from a reused payload.
        cleaned_data.pop("timestamp_correction", None)

    # 4. Return the validation result.
    # Correcting a timestamp does NOT make missing required fields valid.
    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "cleaned_data": cleaned_data,
    }
