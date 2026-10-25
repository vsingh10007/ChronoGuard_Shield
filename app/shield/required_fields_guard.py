
class RequiredFieldsGuard:
    """Detect missing or empty required fields in a request."""

    @staticmethod
    def validate(data, required_fields):
        if not isinstance(data, dict):
            return {
                "valid": False,
                "error": "Request data must be an object",
                "missing_fields": [],
            }

        missing_fields = [
            field
            for field in required_fields
            if field not in data
            or data[field] is None
            or (
                isinstance(data[field], str)
                and not data[field].strip()
            )
        ]

        if missing_fields:
            return {
                "valid": False,
                "error": "Required fields are missing or empty",
                "missing_fields": missing_fields,
            }

        return {
            "valid": True,
            "error": None,
            "missing_fields": [],
        }

