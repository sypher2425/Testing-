"""Single home for dataset timestamp generation and temporal precision.

Every processing timestamp the pipeline writes must be ISO 8601 with an
explicit UTC offset (``+00:00`` or ``Z``). Naive strings imported from
older datasets are normalized (assumed UTC) with a compatibility warning
rather than rejected — old ZIPs must keep loading.

Temporal precision is a separate vocabulary from the metric precision in
``utils/precision.py``: a posting date like ``2026-07-23`` is a calendar
date, not an exact instant, and must say so.
"""
from datetime import datetime, timezone

EXACT_DATETIME = "exact_datetime"
MINUTE = "minute"
HOUR = "hour"
DATE_ONLY = "date_only"
MONTH_ONLY = "month_only"
UNKNOWN = "unknown"

TEMPORAL_PRECISIONS = {EXACT_DATETIME, MINUTE, HOUR, DATE_ONLY, MONTH_ONLY, UNKNOWN}

# Ordered coarsest → finest; used to decide whether an incoming value is
# strictly more precise than a stored one (unknown never wins).
_FINENESS = [UNKNOWN, MONTH_ONLY, DATE_ONLY, HOUR, MINUTE, EXACT_DATETIME]


def now_utc_iso() -> str:
    """The one blessed way to stamp 'now' into a dataset file."""
    return datetime.now(timezone.utc).isoformat()


def ensure_aware_iso(value: str | datetime | None) -> tuple[str | None, bool]:
    """Normalize a timestamp to an offset-aware ISO string.

    Returns ``(normalized, was_naive)``. Naive inputs are assumed UTC —
    that matches how the DB layer stores them — and get ``+00:00``
    attached. Unparseable strings are returned unchanged with
    ``was_naive=False`` so a caller never loses the original value.
    """
    if value is None:
        return None, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat(), True
        return value.isoformat(), False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value, False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc).isoformat(), True
    return value, False


def is_aware_iso(value) -> bool:
    """Validator predicate: does this string carry an explicit offset?

    Absent (None) values return True — "missing" is a different finding
    from "naive". Non-strings and unparseable strings return False.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def infer_date_precision(value: str | None) -> str:
    """Best-supported precision honestly claimable for a date/time string.

    ``YYYY-MM`` → month_only, ``YYYY-MM-DD`` → date_only, ISO datetimes →
    minute or exact_datetime depending on whether seconds are present.
    Anything else → unknown. Never guesses finer than the string shows.
    """
    if not value or not isinstance(value, str):
        return UNKNOWN
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m")
        return MONTH_ONLY
    except ValueError:
        pass
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return UNKNOWN
    if len(text) == 10:  # bare YYYY-MM-DD parses but carries no time
        return DATE_ONLY
    # fromisoformat accepted it, so there is a time component.
    time_part = text[11:]
    return MINUTE if time_part.count(":") == 1 else EXACT_DATETIME


def is_finer(candidate: str, existing: str) -> bool:
    """True when `candidate` is a strictly more precise temporal claim."""
    if candidate not in TEMPORAL_PRECISIONS or existing not in TEMPORAL_PRECISIONS:
        return False
    return _FINENESS.index(candidate) > _FINENESS.index(existing)
