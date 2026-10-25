import logging

logger = logging.getLogger("chronoguard.shield")


class ExceptionGuard:
    @staticmethod
    def execute(operation, *args, **kwargs):
        try:
            result = operation(*args, **kwargs)

            return {
                "success": True,
                "result": result,
                "error": None,
            }

        except Exception as error:
            # Keep technical details in server logs, not the response.
            logger.exception("ChronoGuard operation failed")

            return {
                "success": False,
                "result": None,
                "error": "The operation failed safely",
                "error_type": type(error).__name__,
            }