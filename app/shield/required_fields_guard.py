
class RequiredFieldsGuard:
    """Detect missing, empty, or incorrectly typed required fields."""

    @staticmethod
    def validate(data, required_fields):
        if not isinstance(data, dict):
            return {
                "valid": False,
                "error": "Request data must be an object.",
                "missing_fields": [],
            }

        missing_fields = []
        invalid_fields = []

        for field in required_fields:
            if field not in data or data[field] is None:
                missing_fields.append(field)
                continue

            value = data[field]

            if not isinstance(value, str):
                invalid_fields.append(field)
            elif not value.strip():
                missing_fields.append(field)

        if missing_fields or invalid_fields:
            messages = []

            if missing_fields:
                messages.append(
                    "Required fields are missing or empty: "
                    + ", ".join(missing_fields)
                )

            if invalid_fields:
                messages.append(
                    "Required fields must be text: "
                    + ", ".join(invalid_fields)
                )

            return {
                "valid": False,
                "error": "; ".join(messages),
                "missing_fields": missing_fields,
                "invalid_fields": invalid_fields,
            }

        return {
            "valid": True,
            "error": None,
            "missing_fields": [],
            "invalid_fields": [],
        }
