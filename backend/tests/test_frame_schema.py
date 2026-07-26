from app.utils.frame_schema import (
    CATEGORY_KEY_EVENT_AFTER,
    CATEGORY_KEY_EVENT_BEFORE,
    CATEGORY_KEY_EVENT_EXACT,
    MODE_ADAPTIVE,
    MODE_DENSE_INTERVAL,
    MODE_KEY_EVENT,
    category_for_key_event_offset,
    is_valid_combo,
)


def test_valid_combos_cover_r1_and_r1_5_frame_groups():
    assert is_valid_combo(MODE_ADAPTIVE, "adaptive")
    assert is_valid_combo(MODE_DENSE_INTERVAL, "opening_dense")
    assert is_valid_combo(MODE_KEY_EVENT, "key_event")
    assert is_valid_combo(MODE_KEY_EVENT, "key_event_before")


def test_r1_contradiction_is_now_rejected():
    """Round 1 wrote (adaptive, opening_dense) for dense frames — that's
    exactly the mismatch the validator should flag going forward."""
    assert not is_valid_combo(MODE_ADAPTIVE, "opening_dense")
    assert not is_valid_combo(MODE_ADAPTIVE, "key_event")


def test_unknown_mode_or_category_rejected():
    assert not is_valid_combo("not_a_mode", "adaptive")
    assert not is_valid_combo(MODE_ADAPTIVE, "not_a_category")


def test_key_event_offset_categorization():
    assert category_for_key_event_offset(-0.25) == CATEGORY_KEY_EVENT_BEFORE
    assert category_for_key_event_offset(0.0) == CATEGORY_KEY_EVENT_EXACT
    assert category_for_key_event_offset(0.25) == CATEGORY_KEY_EVENT_AFTER
