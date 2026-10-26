
class RequiredFieldsGuard:
<<<<<<< HEAD
    """Detect missing, empty, or incorrectly typed required fields."""
=======
    """Detect missing or empty required fields in a request."""
>>>>>>> 0d0498095ed935b7d1ed932bff794720a6000c9c

    @staticmethod
    def validate(data, required_fields):
        if not isinstance(data, dict):
            return {
                "valid": False,
<<<<<<< HEAD
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
=======
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
>>>>>>> 0d0498095ed935b7d1ed932bff794720a6000c9c
            }

        return {
            "valid": True,
            "error": None,
            "missing_fields": [],
<<<<<<< HEAD
            "invalid_fields": [],
        }
=======
        }

>>>>>>> 0d0498095ed935b7d1ed932bff794720a6000c9c
