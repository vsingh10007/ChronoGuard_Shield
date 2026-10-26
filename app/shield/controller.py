
from app.shield import (
    RequiredFieldsGuard,
    InputGuard,
    TimestampGuard,
    LoopGuard,
    ExceptionGuard,
)


class ShieldController:
    """
    Central coordinator for ChronoGuard's protection modules.
    """

    def __init__(self):
        self.required_fields = RequiredFieldsGuard()
        self.input_guard = InputGuard()
        self.timestamp_guard = TimestampGuard()
        self.loop_guard = LoopGuard()
        self.exception_guard = ExceptionGuard()

    def validate_request(self, data, required_fields):
        """
        Decide whether a request passes required-field validation.
        """

        field_result = self.required_fields.validate(
            data,
            required_fields,
        )

        if not field_result["valid"]:
            return {
                "decision": "reject",
                "stage": "validation",
                "reason": field_result["error"],
                "missing_fields": field_result["missing_fields"],
            }

        return {
            "decision": "allow",
            "stage": "validation",
            "reason": "Required fields validated",
        }

    def validate_age(self, value):
        """
        Validate and normalize an age value.
        """

        result = self.input_guard.parse_age(value)

        if not result["valid"]:
            return {
                "decision": "reject",
                "stage": "input_validation",
                "reason": result["error"],
            }

        return {
            "decision": "allow",
            "stage": "input_validation",
            "age": result["age"],
        }
