"""utils/timestamps — the single home for generated stamps and temporal
precision. Naive strings are legacy input, never legal fresh output."""
from datetime import datetime, timezone

from app.utils import timestamps as ts


def test_now_utc_iso_is_timezone_aware():
    stamp = ts.now_utc_iso()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert stamp.endswith("+00:00")


def test_ensure_aware_iso_attaches_utc_to_naive_string():
    normalized, was_naive = ts.ensure_aware_iso("2026-07-23T10:00:19.668895")
    assert was_naive is True
    assert normalized == "2026-07-23T10:00:19.668895+00:00"


def test_ensure_aware_iso_keeps_aware_values_untouched():
    for value in ("2026-07-23T10:00:19+00:00", "2026-07-23T10:00:19Z"):
        normalized, was_naive = ts.ensure_aware_iso(value)
        assert was_naive is False
        assert normalized == value


def test_ensure_aware_iso_handles_datetime_and_none():
    naive = datetime(2026, 7, 23, 10, 0, 19)
    normalized, was_naive = ts.ensure_aware_iso(naive)
    assert was_naive is True
    assert normalized == "2026-07-23T10:00:19+00:00"

    aware = datetime(2026, 7, 23, 10, 0, 19, tzinfo=timezone.utc)
    assert ts.ensure_aware_iso(aware) == ("2026-07-23T10:00:19+00:00", False)
    assert ts.ensure_aware_iso(None) == (None, False)


def test_ensure_aware_iso_returns_garbage_unchanged():
    # Never lose the original value, even when it isn't a timestamp at all.
    assert ts.ensure_aware_iso("not a date") == ("not a date", False)


def test_is_aware_iso_predicate():
    assert ts.is_aware_iso("2026-07-23T10:00:19+00:00") is True
    assert ts.is_aware_iso("2026-07-23T10:00:19Z") is True
    assert ts.is_aware_iso("2026-07-23T10:00:19") is False
    assert ts.is_aware_iso("garbage") is False
    assert ts.is_aware_iso(None) is True  # absent is not the same as naive
    assert ts.is_aware_iso(1234) is False


def test_infer_date_precision_per_shape():
    assert ts.infer_date_precision("2026-07") == ts.MONTH_ONLY
    assert ts.infer_date_precision("2026-07-23") == ts.DATE_ONLY
    assert ts.infer_date_precision("2026-07-23T10:30") == ts.MINUTE
    assert ts.infer_date_precision("2026-07-23T10:30:15") == ts.EXACT_DATETIME
    assert ts.infer_date_precision("2026-07-23T10:30:15.123+00:00") == ts.EXACT_DATETIME
    assert ts.infer_date_precision("July 23rd") == ts.UNKNOWN
    assert ts.infer_date_precision(None) == ts.UNKNOWN
    assert ts.infer_date_precision("") == ts.UNKNOWN


def test_is_finer_ordering():
    assert ts.is_finer(ts.EXACT_DATETIME, ts.DATE_ONLY) is True
    assert ts.is_finer(ts.MINUTE, ts.DATE_ONLY) is True
    assert ts.is_finer(ts.DATE_ONLY, ts.DATE_ONLY) is False
    assert ts.is_finer(ts.MONTH_ONLY, ts.DATE_ONLY) is False
    assert ts.is_finer(ts.UNKNOWN, ts.MONTH_ONLY) is False
    assert ts.is_finer("nonsense", ts.DATE_ONLY) is False
