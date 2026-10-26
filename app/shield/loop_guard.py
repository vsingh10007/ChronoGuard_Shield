import time


class LoopGuard:
    def __init__(self, max_iterations=10, timeout_seconds=5):
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds

    def run(self, operation):
        """
        operation(iteration) should perform one bounded unit of work.
        Return False to stop early; return anything else to continue.
        """

        start_time = time.monotonic()

        for iteration in range(1, self.max_iterations + 1):
            if time.monotonic() - start_time >= self.timeout_seconds:
                return {
                    "success": False,
                    "iterations": iteration - 1,
                    "reason": "Execution time limit reached",
                }

            try:
                should_continue = operation(iteration)
            except Exception as error:
                return {
                    "success": False,
                    "iterations": iteration,
                    "reason": f"Operation failed: {type(error).__name__}",
                }

            if should_continue is False:
                return {
                    "success": True,
                    "iterations": iteration,
                    "reason": "Operation completed",
                }

        return {
            "success": False,
            "iterations": self.max_iterations,
            "reason": "Maximum iteration count reached",
        }