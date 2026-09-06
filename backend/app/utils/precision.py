"""Metric precision + provenance vocabulary.

Round 1 stored every extracted number as if it were exact. In reality some
platforms (notably Instagram) render only rounded public counts, and any
future screenshot-based extraction will produce estimates. The precision
field lets a downstream consumer weigh comparisons correctly without us
having to fabricate an accuracy signal we can't verify.

Round 1.5 default: everything yt-dlp returns is marked `exact`. That is
what the extractor claims to give; a future round can flip individual
metrics to `rounded` when the platform's own UI display is the ground
truth (rather than an internal API count). Manual entry and Round 2
screenshot flows are the honest paths to `estimated` /
`screenshot_estimated`. The field exists; guessing does not.
"""
EXACT = "exact"
ROUNDED = "rounded"
ESTIMATED = "estimated"
SCREENSHOT_ESTIMATED = "screenshot_estimated"
UNKNOWN = "unknown"

ALL_PRECISIONS = {EXACT, ROUNDED, ESTIMATED, SCREENSHOT_ESTIMATED, UNKNOWN}

# Ordered worst → best; used when combining input precisions into a rate.
_ORDER = [UNKNOWN, ESTIMATED, SCREENSHOT_ESTIMATED, ROUNDED, EXACT]


def worst_of(*precisions: str) -> str:
    """Returns the least-precise value from the arguments — the correct
    precision for a rate calculated from mixed-precision inputs."""
    filtered = [p for p in precisions if p in ALL_PRECISIONS]
    if not filtered:
        return UNKNOWN
    return min(filtered, key=_ORDER.index)


def derived_label(*input_precisions: str) -> str:
    """Human-readable precision for a computed value: `derived_from_<worst>`
    unless every input is `exact`, in which case the result is `exact` too."""
    combined = worst_of(*input_precisions)
    return EXACT if combined == EXACT else f"derived_from_{combined}"


def is_valid_precision_label(label) -> bool:
    """True for the base vocabulary plus the `derived_from_<p>` labels that
    `derived_label` emits on computed rates."""
    if not isinstance(label, str):
        return False
    if label in ALL_PRECISIONS:
        return True
    prefix = "derived_from_"
    return label.startswith(prefix) and label[len(prefix):] in ALL_PRECISIONS
