"""Frame `mode` and `category` enums, plus the valid combinations.

Round 1 stored dense/key-event frames with `mode: "adaptive"` (inherited
from the job option) while `category: "opening_dense"` / `"key_event"` —
contradictory. This module defines what's valid; the validator flags any
frame whose (mode, category) isn't in `VALID_COMBOS`.
"""

# --- modes (how the frame timestamp was chosen) ---
MODE_ADAPTIVE = "adaptive"
MODE_INTERVAL = "interval"
MODE_PER_SECOND = "per_second"
MODE_EVERY_FRAME = "every_frame"
MODE_DENSE_INTERVAL = "dense_interval"
MODE_KEY_EVENT = "key_event"
MODE_MANUAL = "manual"
MODE_IMPORTED = "imported"

MODES = {
    MODE_ADAPTIVE,
    MODE_INTERVAL,
    MODE_PER_SECOND,
    MODE_EVERY_FRAME,
    MODE_DENSE_INTERVAL,
    MODE_KEY_EVENT,
    MODE_MANUAL,
    MODE_IMPORTED,
}

# --- categories (what group the frame belongs to for analysis) ---
CATEGORY_ADAPTIVE = "adaptive"
CATEGORY_OPENING_DENSE = "opening_dense"
CATEGORY_KEY_EVENT = "key_event"
CATEGORY_KEY_EVENT_BEFORE = "key_event_before"
CATEGORY_KEY_EVENT_EXACT = "key_event_exact"
CATEGORY_KEY_EVENT_AFTER = "key_event_after"
CATEGORY_MANUAL_REFERENCE = "manual_reference"

CATEGORIES = {
    CATEGORY_ADAPTIVE,
    CATEGORY_OPENING_DENSE,
    CATEGORY_KEY_EVENT,
    CATEGORY_KEY_EVENT_BEFORE,
    CATEGORY_KEY_EVENT_EXACT,
    CATEGORY_KEY_EVENT_AFTER,
    CATEGORY_MANUAL_REFERENCE,
}

VALID_COMBOS: set[tuple[str, str]] = {
    # Adaptive-mode selections all live in the adaptive category regardless
    # of whether the user picked adaptive/interval/per_second/every_frame at
    # upload — they're all "the main frame series".
    (MODE_ADAPTIVE, CATEGORY_ADAPTIVE),
    (MODE_INTERVAL, CATEGORY_ADAPTIVE),
    (MODE_PER_SECOND, CATEGORY_ADAPTIVE),
    (MODE_EVERY_FRAME, CATEGORY_ADAPTIVE),
    # Dense opening frames: one mode, one category.
    (MODE_DENSE_INTERVAL, CATEGORY_OPENING_DENSE),
    # Key-event frames: one mode, four accepted categories (the generic one
    # + before/exact/after specializations used by Round 2 when needed).
    (MODE_KEY_EVENT, CATEGORY_KEY_EVENT),
    (MODE_KEY_EVENT, CATEGORY_KEY_EVENT_BEFORE),
    (MODE_KEY_EVENT, CATEGORY_KEY_EVENT_EXACT),
    (MODE_KEY_EVENT, CATEGORY_KEY_EVENT_AFTER),
    # Manual / imported frames.
    (MODE_MANUAL, CATEGORY_MANUAL_REFERENCE),
    (MODE_IMPORTED, CATEGORY_ADAPTIVE),
    (MODE_IMPORTED, CATEGORY_MANUAL_REFERENCE),
}


def category_for_key_event_offset(offset_seconds: float) -> str:
    """Maps a signed offset around an event's timestamp to a specific
    category. Callers may still choose the generic CATEGORY_KEY_EVENT — this
    helper is here for downstream code that wants the finer distinction."""
    if offset_seconds < 0:
        return CATEGORY_KEY_EVENT_BEFORE
    if offset_seconds > 0:
        return CATEGORY_KEY_EVENT_AFTER
    return CATEGORY_KEY_EVENT_EXACT


def is_valid_combo(mode: str, category: str) -> bool:
    return (mode, category) in VALID_COMBOS
