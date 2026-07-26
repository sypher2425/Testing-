"""Focused migration helpers for reading older datasets (schema v1 → v2.2).

This module is the single home for legacy-manifest handling — readers call
`normalize_manifest` / `normalize_frames` instead of scattering per-version
special cases. Normalization is purely in-memory: original files and old
ZIPs are never modified. Anything surprising in a legacy dataset produces
a compatibility *warning*, never a load failure.
"""
import copy

from app.utils import status as st
from app.utils.frame_schema import describe_frame
from app.utils.timestamps import ensure_aware_iso, infer_date_precision


def detect_schema_version(manifest: dict | None) -> tuple[int, int]:
    """Parsed (major, minor) of dataset_schema_version; absent or
    unparseable → (1, 0), the pre-versioning schema."""
    raw = (manifest or {}).get("dataset_schema_version")
    if not raw:
        return (1, 0)
    parts = str(raw).split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (1, 0)
    return (major, minor)


# Manifest paths of every timestamp the pipeline generates. posting dates
# are deliberately absent — they are calendar data with their own precision
# model, not processing stamps.
_TIMESTAMP_PATHS = (
    ("processing", "started_at"),
    ("processing", "manifest_generated_at"),
    ("identity", "exported_at"),
    ("performance_snapshot", "fetched_at"),
    ("comments", "attempted_at"),
    ("validation", "validated_at"),
)


def normalize_frames(frames: list[dict] | None) -> list[dict]:
    """Bring v1 frame entries up to the current in-memory shape: every
    frame gets a category (v1 frames were all adaptive), a mode, and a
    description derived from its own metadata. Input list is not mutated."""
    normalized = []
    for frame in frames or []:
        entry = dict(frame)
        entry.setdefault("category", "adaptive")
        entry.setdefault("mode", "adaptive")
        entry.setdefault("description", describe_frame(entry))
        normalized.append(entry)
    return normalized


def upgrade_posted_at_record(value) -> dict:
    """Normalize any historical posted_at shape into the v2.2 record.

    v1/v2.0 stored a bare string or a {value, status, source: "auto"}
    record; v2.2 wants explicit provenance, entry method, temporal
    precision, and timezone."""
    if isinstance(value, dict):
        record = dict(value)
    elif value in (None, ""):
        return st.field_result(None, st.NOT_AVAILABLE, reason="Not present in source metadata")
    else:
        record = {"value": value, "status": st.SUCCESS, "source": st.SOURCE_AUTO}

    source, entry_method = st.map_legacy_source(record.get("source"), automatic_source=st.SOURCE_YT_DLP)
    record["source"] = source
    record.setdefault("entry_method", entry_method)
    if record.get("value") not in (None, "") and not record.get("precision"):
        record["precision"] = infer_date_precision(record["value"])
    record.setdefault("timezone", None)
    return record


def normalize_manifest(manifest: dict) -> tuple[dict, list[dict]]:
    """Deep-copies and upgrades a manifest of any schema version to the
    current in-memory representation. Returns (normalized, compat_warnings);
    each warning is {"code", "message"} and never blocks loading."""
    normalized = copy.deepcopy(manifest or {})
    warnings: list[dict] = []

    # --- dense config: nested block is canonical; synthesize it from the
    # legacy flat keys when a pre-2.1 manifest only has those.
    params = normalized.get("extraction_params")
    if isinstance(params, dict) and "opening_dense" not in params:
        flat_duration = params.get("opening_dense_duration")
        flat_interval = params.get("opening_dense_interval")
        enabled = params.get("opening_dense_enabled")
        if flat_duration is not None or flat_interval is not None or enabled is not None:
            params["opening_dense"] = {
                "enabled": bool(enabled) if enabled is not None else True,
                "duration_seconds": flat_duration,
                "interval_seconds": flat_interval,
                "source": "unknown",
            }
            warnings.append(
                {
                    "code": "legacy_dense_config_normalized",
                    "message": "Nested opening_dense block synthesized from deprecated flat keys",
                }
            )

    # --- posting date: upgrade to the v2.2 precision-aware record.
    posting_context = normalized.get("posting_context")
    if isinstance(posting_context, dict) and "posted_at" in posting_context:
        old = posting_context["posted_at"]
        upgraded = upgrade_posted_at_record(old)
        if upgraded != old:
            posting_context["posted_at"] = upgraded
            warnings.append(
                {
                    "code": "legacy_posted_at_normalized",
                    "message": "posted_at upgraded to the precision-aware v2.2 record",
                }
            )

    # --- timestamps: attach UTC to naive stamps (older datasets wrote
    # processing.started_at without an offset). Warn, never fail.
    for section, key in _TIMESTAMP_PATHS:
        block = normalized.get(section)
        if not isinstance(block, dict):
            continue
        stamp = block.get(key)
        if not isinstance(stamp, str):
            continue
        aware, was_naive = ensure_aware_iso(stamp)
        if was_naive:
            block[key] = aware
            warnings.append(
                {
                    "code": "naive_timestamp_normalized",
                    "message": f"{section}.{key} had no UTC offset; normalized assuming UTC",
                }
            )

    return normalized, warnings
