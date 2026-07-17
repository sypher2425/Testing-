class PipelineFailedError(Exception):
    """Raised by a PipelineStep to fail the job with a typed, user-readable reason."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
