from app.utils import precision


def test_worst_of_returns_least_precise():
    assert precision.worst_of("exact", "exact") == "exact"
    assert precision.worst_of("exact", "rounded") == "rounded"
    assert precision.worst_of("rounded", "screenshot_estimated") == "screenshot_estimated"
    assert precision.worst_of("exact", "unknown") == "unknown"


def test_worst_of_ignores_invalid_and_missing():
    assert precision.worst_of("exact", None, "not_a_precision") == "exact"
    assert precision.worst_of() == "unknown"


def test_derived_label_all_exact_stays_exact():
    assert precision.derived_label("exact", "exact") == "exact"


def test_derived_label_marks_derived_from_worst_input():
    assert precision.derived_label("rounded", "exact") == "derived_from_rounded"
    assert precision.derived_label("exact", "screenshot_estimated") == "derived_from_screenshot_estimated"
