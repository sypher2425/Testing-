"""Dataset schema v2 builders: engagement breakdown, posting context,
events, and the machine-readable analysis summary.

All builders follow the same rules: never fabricate a value, never coerce a
missing value to 0, and label every value with a status + source
(see app.utils.status).
"""
from app.utils import precision, status as st, timestamps as ts_util

DATASET_SCHEMA_VERSION = "2.2"

# Engagement metrics beyond what platforms expose publicly — these can only
# come from the creator's own analytics (manual entry / future enrichment).
_MANUAL_ONLY_METRICS = (
    "saves",
    "reposts",
    "profile_visits",
    "new_followers",
    "link_clicks",
    "conversion_events",
    "forgegui_clicks",
)

_RATE_DEFINITIONS = {
    "like_rate": "likes",
    "comment_rate": "comments",
    "share_rate": "shares",
    "save_rate": "saves",
    "repost_rate": "reposts",
    "follow_conversion_rate": "new_followers",
}

# Analysis-summary fields derived from the first event of a given type.
_EVENT_TIME_FIELDS = {
    "object_first_visible_seconds": "object_first_visible",
    "first_interaction_seconds": "first_interaction",
    "first_satisfying_reaction_seconds": "first_satisfying_reaction",
    "cta_start_seconds": "cta_start",
}

_EVENT_FLAG_FIELDS = {
    "reference_image_used": "reference_image_added",
    "second_twist_used": "second_twist",
    "counter_used": "counter_appears",
    "celebration_used": "celebration",
}

KNOWN_EVENT_TYPES = {
    "spoken_hook", "first_build_command", "object_first_visible", "first_interaction",
    "first_satisfying_reaction", "sound_added", "effect_added", "more_objects_added",
    "color_change", "counter_appears", "celebration", "intensity_upgrade",
    "hundred_x_cooler", "letdown", "reference_image_added", "reference_transformation",
    "reaction", "second_twist", "cta_start", "video_end",
}


def build_engagement_breakdown(performance: dict | None) -> dict:
    """analytics/performance.json payload: every engagement metric as
    {value, status, source, reason?}, plus rates computed only when both the
    numerator and views are valid numbers."""
    performance = performance or {}
    fields_status = performance.get("fields_status") or {}

    def metric_from_performance(perf_key: str) -> dict:
        value = performance.get(perf_key)
        meta = fields_status.get(perf_key) or {}
        if value is not None:
            # v2.2: provenance (source) is separate from entry method. New
            # fields_status records already carry both; legacy "auto"/"manual"
            # labels are mapped forward here so re-running an old job comes
            # out in the current vocabulary.
            source, entry_method = st.map_legacy_source(
                meta.get("source") or st.SOURCE_AUTO, automatic_source=st.SOURCE_YT_DLP
            )
            result = st.field_result(
                value, st.SUCCESS, source=source, entry_method=meta.get("entry_method") or entry_method
            )
            # R1.5: precision provenance. yt-dlp values are treated as exact
            # (that's what the extractor claims); manual entries carry
            # whatever their own status_record specifies. No auto-guessing.
            result["precision"] = meta.get("precision", precision.EXACT)
            if "display_value" in meta:
                result["display_value"] = meta["display_value"]
            return result
        return st.field_result(
            None,
            meta.get("status", st.NOT_AVAILABLE),
            source=None,
            reason=meta.get("reason", "Not exposed by the platform"),
        )

    metrics = {
        "views": metric_from_performance("view_count"),
        "likes": metric_from_performance("like_count"),
        "comments": metric_from_performance("comment_count"),
        "shares": metric_from_performance("share_count"),
    }
    for name in _MANUAL_ONLY_METRICS:
        metrics[name] = st.field_result(
            None,
            st.MANUAL_REQUIRED,
            reason="Only visible in the creator's own analytics; supply manually",
        )

    rate_numerator_sources = {
        "like_rate": metrics["likes"],
        "comment_rate": metrics["comments"],
        "share_rate": metrics["shares"],
        "save_rate": metrics["saves"],
        "repost_rate": metrics["reposts"],
        "follow_conversion_rate": metrics["new_followers"],
    }
    views_value = metrics["views"]["value"]
    views_precision = metrics["views"].get("precision", precision.UNKNOWN)
    rate_field_map = {
        "like_rate": "likes",
        "comment_rate": "comments",
        "share_rate": "shares",
        "save_rate": "saves",
        "repost_rate": "reposts",
        "follow_conversion_rate": "new_followers",
    }
    rates: dict = {}
    for rate_name, numerator in rate_numerator_sources.items():
        num_value = numerator["value"]
        numerator_field = rate_field_map[rate_name]
        if isinstance(num_value, (int, float)) and isinstance(views_value, (int, float)) and views_value > 0:
            num_precision = numerator.get("precision", precision.UNKNOWN)
            derived = precision.derived_label(num_precision, views_precision)
            # If any input is imprecise, don't fake six decimals of precision.
            decimals = 6 if derived == precision.EXACT else 4
            rates[rate_name] = {
                "value": round(num_value / views_value, decimals),
                "status": st.CALCULATED,
                "precision": derived,
                "numerator_field": numerator_field,
                "denominator_field": "views",
            }
        else:
            rates[rate_name] = {
                "value": None,
                "status": st.NOT_AVAILABLE,
                "precision": precision.UNKNOWN,
                "numerator_field": numerator_field,
                "denominator_field": "views",
                "reason": "Requires both a valid numerator and a valid view count",
            }

    return {"metrics": metrics, "rates": rates}


def build_posted_at(value, fields_from_label: str | None = None) -> dict:
    """v2.2 posting-date record. A calendar date is not an exact timestamp:
    the record declares its temporal precision (yt-dlp's upload_date is a
    bare date → date_only) and its timezone (null — platforms don't expose
    it), so a consumer never mistakes 2026-07-23 for an exact instant."""
    if value in (None, ""):
        return st.field_result(None, st.NOT_AVAILABLE, reason="Not present in source metadata")
    if fields_from_label == "manual":
        source, entry = st.SOURCE_USER, st.ENTRY_MANUAL
    else:
        source, entry = st.SOURCE_YT_DLP, st.ENTRY_AUTOMATIC
    record = st.field_result(
        value,
        st.SUCCESS,
        source=source,
        entry_method=entry,
        precision=ts_util.infer_date_precision(value),
    )
    record["timezone"] = None
    return record


def upgrade_posted_at(existing: dict | None, incoming: dict | None) -> dict:
    """Provenance-preserving posting-date upgrade (the one blessed path for
    Round 2 enrichment). The incoming record wins only when it is a strictly
    finer temporal claim (or the existing slot is empty); the superseded
    record is kept in `provenance` so the original source is never lost."""
    existing = dict(existing or {})
    if not isinstance(incoming, dict) or incoming.get("value") in (None, ""):
        return existing
    if existing.get("value") in (None, ""):
        return dict(incoming)
    incoming_precision = incoming.get("precision") or ts_util.UNKNOWN
    existing_precision = existing.get("precision") or ts_util.UNKNOWN
    if not ts_util.is_finer(incoming_precision, existing_precision):
        return existing
    upgraded = dict(incoming)
    superseded = {
        key: existing.get(key) for key in ("value", "precision", "source", "entry_method")
    }
    superseded["recorded_at"] = ts_util.now_utc_iso()
    upgraded["provenance"] = [*(existing.get("provenance") or []), superseded]
    return upgraded


def build_posting_context(performance: dict | None, source_url: str | None) -> dict:
    """Auto-fills what source metadata provides; everything else is an explicit
    manual_required slot for the enrichment flow."""
    performance = performance or {}
    fields_from = performance.get("fields_from") or {}

    def auto(value):
        if value not in (None, "", []):
            return st.field_result(value, st.SUCCESS, source=st.SOURCE_YT_DLP, entry_method=st.ENTRY_AUTOMATIC)
        return st.field_result(None, st.NOT_AVAILABLE, reason="Not present in source metadata")

    manual = st.field_result(None, st.MANUAL_REQUIRED, reason="Not exposed by platforms; enter manually")
    return {
        "platform": auto(performance.get("platform") if performance.get("platform") != "manual" else None),
        "account_handle": auto(performance.get("uploader")),
        "account_id": manual,
        "posted_at": build_posted_at(performance.get("upload_date"), fields_from.get("upload_date")),
        "timezone": manual,
        "follower_count_at_posting": manual,
        "account_age": manual,
        "account_stage": manual,  # new | warmed_up | established
        "sponsored": manual,
        "repost_or_original": manual,
        "target_audience": manual,
        "audience_countries": manual,
        "tier1_audience_percent": manual,
        "traffic_sources": manual,
        "video_language": manual,  # detected transcript language lands in manifest.language already
        "geo_tag": manual,
        "music_added_in_app": manual,
        "edited_after_publishing": manual,
        "source_url": auto(source_url),
    }


def normalize_events(raw_events: list | None) -> list[dict]:
    """Validates/normalizes user-supplied events. Unknown types are allowed
    (kept verbatim); entries without a numeric time are dropped."""
    normalized = []
    for i, event in enumerate(raw_events or []):
        if not isinstance(event, dict):
            continue
        time_seconds = event.get("time_seconds")
        if not isinstance(time_seconds, (int, float)) or time_seconds < 0:
            continue
        normalized.append(
            {
                "id": event.get("id") or f"event_{i + 1:03d}",
                "time_seconds": round(float(time_seconds), 3),
                "end_time_seconds": (
                    round(float(event["end_time_seconds"]), 3)
                    if isinstance(event.get("end_time_seconds"), (int, float))
                    else None
                ),
                "type": event.get("type") or "custom",
                "label": event.get("label") or event.get("type") or "event",
                "source": event.get("source") or st.SOURCE_MANUAL,
                "confidence": event.get("confidence", 1.0 if event.get("source", "manual") == "manual" else None),
                "transcript_text": event.get("transcript_text"),
            }
        )
    return normalized


def build_analysis_summary(events: list[dict], audio_stats: dict | None) -> dict:
    """Machine-readable cross-video comparison block. Only values actually
    derivable from present data get a value; everything else stays null with
    a status — never guessed."""
    summary: dict = {}

    def first_event_of(event_type: str) -> dict | None:
        matches = [e for e in events if e.get("type") == event_type]
        return min(matches, key=lambda e: e["time_seconds"]) if matches else None

    for field_name, event_type in _EVENT_TIME_FIELDS.items():
        event = first_event_of(event_type)
        if event:
            summary[field_name] = st.field_result(event["time_seconds"], st.SUCCESS, source=event.get("source"))
        else:
            summary[field_name] = st.field_result(
                None, st.MANUAL_REQUIRED, reason=f"No '{event_type}' event has been annotated"
            )

    for field_name, event_type in _EVENT_FLAG_FIELDS.items():
        if events:
            event = first_event_of(event_type)
            summary[field_name] = st.field_result(
                event is not None, st.SUCCESS, source=event.get("source") if event else st.SOURCE_MANUAL
            )
        else:
            summary[field_name] = st.field_result(
                None, st.MANUAL_REQUIRED, reason="No events have been annotated"
            )

    if events:
        summary["major_build_beats"] = st.field_result(len(events), st.SUCCESS, source=st.SOURCE_MANUAL)
    else:
        summary["major_build_beats"] = st.field_result(None, st.MANUAL_REQUIRED, reason="No events have been annotated")

    audio_stats = audio_stats or {}
    for summary_key, audio_key in (
        ("spoken_word_count", "spoken_word_count"),
        ("overall_wpm", "overall_wpm"),
        ("active_narration_wpm", "active_narration_wpm"),
    ):
        source_field = audio_stats.get(audio_key)
        if source_field is not None:
            summary[summary_key] = source_field
        else:
            summary[summary_key] = st.field_result(None, st.NOT_AVAILABLE, reason="Audio stats unavailable")

    return summary
