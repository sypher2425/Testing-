"""Shared extraction-status vocabulary for dataset schema v2.

Every metric or artifact that can't be extracted is stored as null plus an
explicit status and reason — never silently converted to 0 or an empty
array. AI-estimated and screenshot-estimated sources are defined now (as
reserved values) so wiring those features later is purely additive.
"""

SUCCESS = "success"
CALCULATED = "calculated"
PARTIAL = "partial"
NOT_AVAILABLE = "not_available"
UNSUPPORTED = "unsupported"
AUTHENTICATION_REQUIRED = "authentication_required"
RATE_LIMITED = "rate_limited"
EXTRACTION_FAILED = "extraction_failed"
MANUAL_REQUIRED = "manual_required"
MANUAL_UNAVAILABLE = "manual_unavailable"
NO_COMMENTS = "no_comments"
COMMENTS_DISABLED = "comments_disabled"
UNEXPECTED_EMPTY_RESULT = "unexpected_empty_result"
SKIPPED = "skipped"
UNKNOWN = "unknown"

ALL_STATUSES = {
    SUCCESS,
    CALCULATED,
    PARTIAL,
    NOT_AVAILABLE,
    UNSUPPORTED,
    AUTHENTICATION_REQUIRED,
    RATE_LIMITED,
    EXTRACTION_FAILED,
    MANUAL_REQUIRED,
    MANUAL_UNAVAILABLE,
    NO_COMMENTS,
    COMMENTS_DISABLED,
    UNEXPECTED_EMPTY_RESULT,
    SKIPPED,
    UNKNOWN,
}

# Where a value truly came from (provenance). Schema v2.2 separates this
# from HOW it was entered (entry_method below): a number scraped by yt-dlp
# and a number a person typed from the analytics screen have different
# provenance even though both may be "the platform's count".
# "ai_estimated" / "screenshot_estimated" are reserved for the deferred AI
# features.
SOURCE_YT_DLP = "yt_dlp"
SOURCE_USER = "user"
SOURCE_COMPUTED = "computed"
SOURCE_AI_ESTIMATED = "ai_estimated"
SOURCE_SCREENSHOT_ESTIMATED = "screenshot_estimated"

# Deprecated pre-2.2 labels. "auto" conflated provenance with entry method;
# kept only so legacy datasets can be read and mapped forward.
SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"

ALL_SOURCES = {SOURCE_YT_DLP, SOURCE_USER, SOURCE_COMPUTED, SOURCE_AI_ESTIMATED, SOURCE_SCREENSHOT_ESTIMATED}

ENTRY_AUTOMATIC = "automatic"
ENTRY_MANUAL = "manual"
ALL_ENTRY_METHODS = {ENTRY_AUTOMATIC, ENTRY_MANUAL}


def map_legacy_source(legacy: str | None, automatic_source: str = SOURCE_YT_DLP) -> tuple[str | None, str | None]:
    """Translate a pre-2.2 source label into (source, entry_method).

    "auto" meant "the pipeline filled this in" — its true provenance is
    yt-dlp for fetched metrics or `computed` for derived stats, which the
    caller knows and passes as `automatic_source`. "manual" meant a person
    typed the value. Already-current labels pass through unchanged.
    """
    if legacy == SOURCE_AUTO:
        return automatic_source, ENTRY_AUTOMATIC
    if legacy == SOURCE_MANUAL:
        return SOURCE_USER, ENTRY_MANUAL
    if legacy in ALL_SOURCES:
        return legacy, None
    return legacy, None


def field_result(
    value,
    status: str,
    source: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    entry_method: str | None = None,
    precision: str | None = None,
) -> dict:
    """Uniform {value, status, source, reason?, error?, entry_method?,
    precision?} record for a single extracted (or unextractable) field.
    The optional keys are only emitted when provided, so fields untouched
    by the v2.2 provenance split keep their exact previous shape."""
    assert status in ALL_STATUSES, f"unknown status: {status}"
    result: dict = {"value": value, "status": status, "source": source}
    if reason is not None:
        result["reason"] = reason
    if error is not None:
        result["error"] = error
    if entry_method is not None:
        result["entry_method"] = entry_method
    if precision is not None:
        result["precision"] = precision
    return result
