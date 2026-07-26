"""Shared extraction-status vocabulary for dataset schema v2.

Every metric or artifact that can't be extracted is stored as null plus an
explicit status and reason — never silently converted to 0 or an empty
array. AI-estimated and screenshot-estimated sources are defined now (as
reserved values) so wiring those features later is purely additive.
"""

SUCCESS = "success"
PARTIAL = "partial"
NOT_AVAILABLE = "not_available"
UNSUPPORTED = "unsupported"
AUTHENTICATION_REQUIRED = "authentication_required"
RATE_LIMITED = "rate_limited"
EXTRACTION_FAILED = "extraction_failed"
MANUAL_REQUIRED = "manual_required"
SKIPPED = "skipped"

ALL_STATUSES = {
    SUCCESS,
    PARTIAL,
    NOT_AVAILABLE,
    UNSUPPORTED,
    AUTHENTICATION_REQUIRED,
    RATE_LIMITED,
    EXTRACTION_FAILED,
    MANUAL_REQUIRED,
    SKIPPED,
}

# Where a value came from. "ai_estimated" / "screenshot_estimated" are
# reserved for the deferred AI features.
SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
SOURCE_AI_ESTIMATED = "ai_estimated"
SOURCE_SCREENSHOT_ESTIMATED = "screenshot_estimated"


def field_result(value, status: str, source: str | None = None, reason: str | None = None, error: str | None = None) -> dict:
    """Uniform {value, status, source, reason?, error?} record for a single
    extracted (or unextractable) field."""
    assert status in ALL_STATUSES, f"unknown status: {status}"
    result: dict = {"value": value, "status": status, "source": source}
    if reason is not None:
        result["reason"] = reason
    if error is not None:
        result["error"] = error
    return result
