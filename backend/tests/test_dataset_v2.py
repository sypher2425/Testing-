"""Dataset schema v2: engagement breakdown, analysis summary, events,
key-event/dense frames, nested-zip guard, manifest v2 shape."""
import json
import zipfile
from unittest.mock import patch

from app.utils import status as st
from app.utils.dataset_v2 import (
    build_analysis_summary,
    build_engagement_breakdown,
    build_posting_context,
    normalize_events,
)
from tests.test_pipeline_steps import make_ctx


# ---------- engagement breakdown ----------


def test_rates_computed_only_when_views_and_numerator_valid():
    performance = {
        "view_count": 1000,
        "like_count": 50,
        "comment_count": None,
        "share_count": None,
        "fields_status": {
            "comment_count": {"status": "extraction_failed", "reason": "not exposed"},
            "share_count": {"status": "not_available", "reason": "no such metric"},
        },
    }
    result = build_engagement_breakdown(performance)

    assert result["metrics"]["views"]["value"] == 1000
    assert result["rates"]["like_rate"]["value"] == 0.05
    # Missing numerator -> null rate with a reason, never 0.
    assert result["rates"]["comment_rate"]["value"] is None
    assert result["rates"]["comment_rate"]["status"] == st.NOT_AVAILABLE
    # Manual-only metrics are explicit manual_required slots.
    assert result["metrics"]["saves"]["status"] == st.MANUAL_REQUIRED
    assert result["metrics"]["forgegui_clicks"]["status"] == st.MANUAL_REQUIRED


def test_rates_null_when_views_missing():
    performance = {"view_count": None, "like_count": 50, "comment_count": 2, "share_count": None, "fields_status": {}}
    result = build_engagement_breakdown(performance)
    assert result["rates"]["like_rate"]["value"] is None
    assert result["metrics"]["likes"]["value"] == 50


def test_metric_statuses_flow_through_from_fields_status():
    performance = {
        "view_count": None,
        "like_count": None,
        "comment_count": None,
        "share_count": None,
        "fields_status": {"view_count": {"status": "extraction_failed", "reason": "The platform did not expose this field"}},
    }
    result = build_engagement_breakdown(performance)
    views = result["metrics"]["views"]
    assert views["value"] is None
    assert views["status"] == "extraction_failed"
    assert "did not expose" in views["reason"]


# ---------- events + analysis summary ----------


def test_normalize_events_drops_invalid_and_fills_defaults():
    events = normalize_events(
        [
            {"time_seconds": 1.8, "type": "object_first_visible", "label": "Pop-its appear"},
            {"type": "no_time_so_dropped"},
            {"time_seconds": -5, "type": "negative_dropped"},
            {"time_seconds": 3.4, "type": "first_interaction", "id": "my_id", "confidence": 0.9, "source": "manual"},
        ]
    )
    assert len(events) == 2
    assert events[0]["id"] == "event_001"
    assert events[0]["source"] == "manual"
    assert events[0]["confidence"] == 1.0
    assert events[1]["id"] == "my_id"
    assert events[1]["confidence"] == 0.9


def test_analysis_summary_from_events_and_audio():
    events = normalize_events(
        [
            {"time_seconds": 1.8, "type": "object_first_visible"},
            {"time_seconds": 3.4, "type": "first_interaction"},
            {"time_seconds": 20.0, "type": "counter_appears"},
        ]
    )
    audio = {
        "spoken_word_count": {"value": 87, "status": "success", "source": "auto"},
        "overall_wpm": {"value": 168, "status": "success", "source": "auto"},
        "active_narration_wpm": {"value": 205, "status": "success", "source": "auto"},
    }
    summary = build_analysis_summary(events, audio)

    assert summary["object_first_visible_seconds"]["value"] == 1.8
    assert summary["first_interaction_seconds"]["value"] == 3.4
    assert summary["counter_used"]["value"] is True
    assert summary["second_twist_used"]["value"] is False
    assert summary["major_build_beats"]["value"] == 3
    assert summary["spoken_word_count"]["value"] == 87
    # No CTA event annotated -> explicit manual_required, not a guess.
    assert summary["cta_start_seconds"]["value"] is None
    assert summary["cta_start_seconds"]["status"] == st.MANUAL_REQUIRED


def test_analysis_summary_with_no_events_never_fabricates():
    summary = build_analysis_summary([], None)
    assert summary["object_first_visible_seconds"]["value"] is None
    assert summary["counter_used"]["value"] is None
    assert summary["major_build_beats"]["value"] is None


def test_posting_context_autofills_only_source_metadata():
    ctx = build_posting_context(
        {"platform": "youtube", "uploader": "SomeChannel", "upload_date": "2026-01-01"},
        "https://youtu.be/xyz",
    )
    assert ctx["platform"]["value"] == "youtube"
    assert ctx["account_handle"]["value"] == "SomeChannel"
    assert ctx["follower_count_at_posting"]["status"] == st.MANUAL_REQUIRED
    assert ctx["sponsored"]["status"] == st.MANUAL_REQUIRED


# ---------- key-event frames ----------


def test_key_event_frames_extracted_around_events(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, _, _ = make_ctx(
        tmp_path,
        options={
            "mode": "interval",
            "interval_ms": 1000,
            "frame_format": "jpeg",
            "frame_max_dim": 1280,
            "opening_dense_enabled": False,
            "events": [{"id": "event_001", "time_seconds": 2.0, "type": "first_interaction", "label": "tank crush"}],
        },
    )
    ctx.shared["video"] = {"duration_seconds": 10.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract), patch(
        "app.pipeline.steps.extract_frames._average_hash", return_value=None
    ):
        ExtractFramesStep().run(ctx)

    key_frames = [f for f in ctx.shared["frames"] if f["category"] == "key_event"]
    assert [f["timestamp"] for f in key_frames] == [1.75, 2.0, 2.25]
    assert all(f["event_id"] == "event_001" for f in key_frames)
    assert all(f["image"].startswith("key_events/") for f in key_frames)
    assert ctx.shared["frame_counts_by_category"]["key_events"] == 3
    # Dense disabled for this job
    assert ctx.shared["frame_counts_by_category"]["opening_dense"] == 0


def test_key_event_near_duplicate_frames_suppressed(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, logs, _ = make_ctx(
        tmp_path,
        options={
            "mode": "interval",
            "interval_ms": 1000,
            "frame_format": "jpeg",
            "frame_max_dim": 1280,
            "opening_dense_enabled": False,
            "events": [{"time_seconds": 2.0, "type": "first_interaction"}],
        },
    )
    ctx.shared["video"] = {"duration_seconds": 10.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    # Identical hash for every frame -> only the first key-event frame is kept.
    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract), patch(
        "app.pipeline.steps.extract_frames._average_hash", return_value=0xABCDEF
    ):
        ExtractFramesStep().run(ctx)

    key_frames = [f for f in ctx.shared["frames"] if f["category"] == "key_event"]
    assert len(key_frames) == 1
    assert key_frames[0]["phash"] == format(0xABCDEF, "016x")


# ---------- nested dataset guard ----------


def test_zip_excludes_nested_zips_and_datasets_without_deleting(tmp_path):
    from app.pipeline.steps.zip_output import ZipOutputStep

    ctx, logs, _ = make_ctx(tmp_path)
    ctx.storage.save_bytes(ctx.job_relative("manifest.json"), b"{}")
    ctx.storage.save_bytes(ctx.job_relative("frames", "0000.000.jpg"), b"jpg")
    # Plant a stray dataset zip and a full nested dataset directory.
    ctx.storage.save_bytes(ctx.job_relative("old-dataset.zip"), b"PK\x03\x04junk")
    ctx.storage.save_bytes(ctx.job_relative("accidental", "manifest.json"), b"{}")
    ctx.storage.save_bytes(ctx.job_relative("accidental", "source", "video.mp4"), b"vid")

    ZipOutputStep().run(ctx)

    zip_path = ctx.storage.get(ctx.job_relative("output.zip"))
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "frames/0000.000.jpg" in names
    assert "old-dataset.zip" not in names
    assert not any(n.startswith("accidental/") for n in names)
    assert "metadata/zip_exclusions.json" in names

    exclusions = json.loads(ctx.storage.get(ctx.job_relative("metadata", "zip_exclusions.json")).read_bytes())
    reasons = {e["reason"] for e in exclusions}
    assert reasons == {"nested_zip_archive", "nested_dataset_directory"}
    # Nothing was deleted from disk.
    assert ctx.storage.exists(ctx.job_relative("old-dataset.zip"))
    assert ctx.storage.exists(ctx.job_relative("accidental", "manifest.json"))
    assert any("Excluding" in m for _, m in logs)


# ---------- manifest v2 ----------


def test_manifest_v2_shape(tmp_path):
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx, _, _ = make_ctx(tmp_path, options={"events": [{"time_seconds": 1.8, "type": "object_first_visible"}]})
    ctx.shared["mode"] = "adaptive"
    ctx.shared["original_filename"] = "clip.mp4"
    ctx.shared["stored_source_filename"] = "video.mp4"
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    ctx.shared["started_at"] = "2026-01-01T00:00:00+00:00"
    ctx.shared["source_sha256"] = "ab" * 32
    ctx.shared["frame_count"] = 1
    ctx.shared["frame_counts_by_category"] = {"adaptive": 1, "opening_dense": 0, "key_events": 0}
    # Stamps must be tz-aware — the v2.2 validator errors on naive ones.
    ctx.shared["stage_reports"] = [
        {
            "stage": "probing",
            "status": "success",
            "started_at": "2026-01-01T00:00:01+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
            "error": None,
        }
    ]
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["video"] = {"duration_seconds": 5.0, "width": 640, "height": 480, "fps": 30.0, "codec": "h264", "has_audio": True}
    ctx.shared["transcript"] = {
        "language": "en",
        "skipped": False,
        "segments": [{"start": 0.0, "end": 2.0, "text": "hello there friend"}],
    }
    for fname in ("transcript.txt", "transcript.json", "subtitles.srt"):
        ctx.storage.save_bytes(ctx.job_relative("transcript", fname), b"x")
    ctx.shared["frames"] = [
        {"frame": 0, "timestamp": 0.0, "image": "0000.000.jpg", "mode": "adaptive", "category": "adaptive", "scene_id": 0}
    ]
    ctx.storage.save_bytes(ctx.job_relative("frames", "0000.000.jpg"), b"jpg")
    ctx.shared["performance"] = {
        "platform": "youtube",
        "uploader": "chan",
        "upload_date": "2026-01-01",
        "description": "my caption #x",
        "view_count": 100,
        "like_count": 5,
        "comment_count": None,
        "share_count": None,
        "fields_status": {"comment_count": {"status": "extraction_failed", "reason": "r"}},
    }

    GenerateMetadataStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    assert manifest["dataset_schema_version"] == "2.2"
    assert manifest["source_video_sha256"] == "ab" * 32
    assert manifest["frame_counts"]["adaptive"] == 1
    assert manifest["total_frame_count"] == 1  # R1.5
    assert manifest["frame_count_legacy_meaning"] == "adaptive_frames_only"
    assert manifest["extraction_report"][0]["stage"] == "probing"
    assert manifest["posting_context"]["platform"]["value"] == "youtube"
    # v2.2: posted_at is a precision-aware record — a calendar date is not an
    # exact timestamp, and provenance/entry method are separate fields.
    posted_at = manifest["posting_context"]["posted_at"]
    assert posted_at["value"] == "2026-01-01"
    assert posted_at["precision"] == "date_only"
    assert posted_at["timezone"] is None
    assert posted_at["source"] == "yt_dlp"
    assert posted_at["entry_method"] == "automatic"
    # v2.2: flat dense keys are back-filled from the canonical nested block.
    params = manifest["extraction_params"]
    assert params["opening_dense_enabled"] == params["opening_dense"]["enabled"]
    assert params["opening_dense_duration"] == params["opening_dense"]["duration_seconds"]
    assert params["opening_dense_interval"] == params["opening_dense"]["interval_seconds"]
    assert "opening_dense_duration" in params["_deprecated"]
    assert manifest["analysis_summary"]["object_first_visible_seconds"]["value"] == 1.8
    assert manifest["events_count"] == 1
    # v1 keys still present and unchanged in meaning.
    assert manifest["frame_count"] == 1
    assert manifest["performance"]["view_count"] == 100
    assert manifest["analyses"] == {}
    # R1.5 additions
    assert manifest["identity"]["dataset_id"] == ctx.job_id
    assert manifest["identity"]["platform"] == "youtube"
    assert manifest["platform_capabilities"]["platform"] == "youtube"
    assert manifest["validation"]["status"] in ("success", "warning")

    # Files written alongside the manifest.
    assert ctx.storage.exists(ctx.job_relative("content", "caption.txt"))
    assert ctx.storage.exists(ctx.job_relative("content", "audio.json"))
    assert ctx.storage.exists(ctx.job_relative("analytics", "performance.json"))
    events = json.loads(ctx.storage.get(ctx.job_relative("events.json")).read_bytes())
    assert events[0]["type"] == "object_first_visible"

    engagement = json.loads(ctx.storage.get(ctx.job_relative("analytics", "performance.json")).read_bytes())
    assert engagement["rates"]["like_rate"]["value"] == 0.05


def test_r15_dense_and_key_event_frames_have_correct_modes(tmp_path):
    """R1.5 regression: dense frames must not be labeled mode=adaptive."""
    from app.pipeline.steps.extract_frames import ExtractFramesStep
    from unittest.mock import patch

    # `interval` mode avoids scene-detect and doesn't need a real file.
    ctx, _, _ = make_ctx(
        tmp_path,
        options={
            "mode": "interval",
            "interval_ms": 1000,
            "frame_format": "jpeg",
            "frame_max_dim": 1280,
            "opening_dense_enabled": True,
            "opening_dense_duration": 2.0,
            "opening_dense_interval": 0.25,
            "events": [{"time_seconds": 3.0, "type": "first_interaction"}],
        },
    )
    ctx.shared["video"] = {"duration_seconds": 10.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpg")

    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract), patch(
        "app.pipeline.steps.extract_frames._average_hash", return_value=None
    ):
        ExtractFramesStep().run(ctx)

    dense = [f for f in ctx.shared["frames"] if f["category"] == "opening_dense"]
    key_events = [f for f in ctx.shared["frames"] if f["category"] == "key_event"]
    assert all(f["mode"] == "dense_interval" for f in dense), "dense frames must use mode=dense_interval, not adaptive"
    assert all(f["mode"] == "key_event" for f in key_events), "key-event frames must use mode=key_event, not adaptive"
    # Effective config was stashed for the manifest.
    assert ctx.shared["opening_dense_config"] == {
        "enabled": True,
        "duration_seconds": 2.0,
        "interval_seconds": 0.25,
        "source": "user_interface",
    }


def test_r15_engagement_rate_uses_calculated_status_and_precision(tmp_path):
    result = build_engagement_breakdown(
        {
            "view_count": 1000,
            "like_count": 50,
            "comment_count": 5,
            "share_count": None,
            "fields_status": {
                "view_count": {"status": "success", "source": "auto", "precision": "exact"},
                "like_count": {"status": "success", "source": "auto", "precision": "exact"},
                "comment_count": {"status": "success", "source": "auto", "precision": "exact"},
            },
        }
    )
    like_rate = result["rates"]["like_rate"]
    assert like_rate["status"] == "calculated"
    assert like_rate["precision"] == "exact"
    assert like_rate["numerator_field"] == "likes"
    assert like_rate["denominator_field"] == "views"
    assert like_rate["value"] == 0.05
    # Views + likes both exact → rate stays exact.


def test_r15_rate_precision_downgrades_with_rounded_input():
    result = build_engagement_breakdown(
        {
            "view_count": 1_000_000,
            "like_count": 16_000,
            "comment_count": None,
            "share_count": None,
            "fields_status": {
                "view_count": {"status": "success", "source": "auto", "precision": "exact"},
                "like_count": {"status": "success", "source": "manual", "precision": "rounded"},
            },
        }
    )
    like_rate = result["rates"]["like_rate"]
    # Not fake six-decimals: derived from a rounded input.
    assert like_rate["precision"] == "derived_from_rounded"
    assert like_rate["value"] == 0.016  # rounded to 4 decimals, not 6


def test_manifest_v2_upload_without_source_platform(tmp_path):
    """Direct file uploads: no comments platform, caption manual_required —
    statuses must explain the absence rather than fake anything."""
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx, _, _ = make_ctx(tmp_path, options={})
    ctx.shared["mode"] = "adaptive"
    ctx.shared["original_filename"] = "clip.mp4"
    ctx.shared["stored_source_filename"] = "video.mp4"
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    ctx.shared["started_at"] = None
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["video"] = {"duration_seconds": 5.0, "width": 1, "height": 1, "fps": 1.0, "codec": "h264", "has_audio": False}
    ctx.shared["transcript"] = {"language": None, "skipped": True, "skipped_reason": "no_audio_track", "segments": []}
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.json"), b"{}")
    ctx.shared["frames"] = []
    ctx.shared["frame_count"] = 0

    GenerateMetadataStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    assert manifest["comments"]["status"] == "not_available"
    assert manifest["content"]["caption"]["status"] == "manual_required"
    extraction_status = json.loads(
        ctx.storage.get(ctx.job_relative("comments", "extraction_status.json")).read_bytes()
    )
    assert extraction_status["status"] == "not_available"


def test_upgrade_posted_at_accepts_only_finer_precision():
    """Provenance preservation: a finer value wins but the superseded record
    is kept; a coarser or equal value never overwrites."""
    from app.utils.dataset_v2 import build_posted_at, upgrade_posted_at

    original = build_posted_at("2026-07-23")  # yt_dlp / automatic / date_only
    finer = {
        "value": "2026-07-23T18:30:00+00:00",
        "status": "success",
        "source": "user",
        "entry_method": "manual",
        "precision": "exact_datetime",
        "timezone": "UTC",
    }
    upgraded = upgrade_posted_at(original, finer)
    assert upgraded["value"] == "2026-07-23T18:30:00+00:00"
    assert upgraded["precision"] == "exact_datetime"
    # The original yt-dlp record survives in provenance.
    assert upgraded["provenance"][0]["value"] == "2026-07-23"
    assert upgraded["provenance"][0]["source"] == "yt_dlp"
    assert "recorded_at" in upgraded["provenance"][0]

    # Coarser (or equal) precision never overwrites.
    coarser = {"value": "2026-07", "status": "success", "precision": "month_only"}
    assert upgrade_posted_at(upgraded, coarser) == upgraded
    same = {"value": "2026-07-24", "status": "success", "precision": "date_only"}
    assert upgrade_posted_at(original, same) == original

    # An empty slot accepts anything with a value.
    empty = {"value": None, "status": "not_available"}
    assert upgrade_posted_at(empty, same)["value"] == "2026-07-24"


def test_posted_at_manual_override_maps_to_user_manual():
    from app.utils.dataset_v2 import build_posted_at

    record = build_posted_at("2026-07-23", "manual")
    assert record["source"] == "user"
    assert record["entry_method"] == "manual"
    assert record["precision"] == "date_only"


def test_engagement_metrics_carry_provenance_not_auto():
    from app.utils.dataset_v2 import build_engagement_breakdown

    breakdown = build_engagement_breakdown(
        {
            "view_count": 100,
            "like_count": 5,
            "fields_status": {
                "view_count": {"status": "success", "source": "yt_dlp", "entry_method": "automatic", "precision": "exact"},
                "like_count": {"status": "success", "source": "auto", "precision": "exact"},  # legacy label
            },
        }
    )
    assert breakdown["metrics"]["views"]["source"] == "yt_dlp"
    assert breakdown["metrics"]["views"]["entry_method"] == "automatic"
    # Legacy "auto" is mapped forward, never written through.
    assert breakdown["metrics"]["likes"]["source"] == "yt_dlp"
    assert breakdown["metrics"]["likes"]["entry_method"] == "automatic"


# ---------- source video excluded from the ZIP ----------


def test_zip_excludes_source_video_but_keeps_the_analysis(tmp_path):
    """The ZIP carries the analysis, not the 60GB source: deflating already-
    compressed video costs hours for ~0% saving and a second full copy."""
    from app.pipeline.steps.zip_output import ZipOutputStep

    ctx, logs, _ = make_ctx(tmp_path)
    ctx.storage.save_bytes(ctx.job_relative("manifest.json"), b"{}")
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"pretend this is 60GB")
    ctx.storage.save_bytes(ctx.job_relative("frames", "0000.000.jpg"), b"jpg")
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.txt"), b"hello")

    ZipOutputStep().run(ctx)

    with zipfile.ZipFile(ctx.storage.get(ctx.job_relative("output.zip"))) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith("source/") for n in names)
    assert {"manifest.json", "frames/0000.000.jpg", "transcript/transcript.txt"} <= names
    # Exactly one manifest, still at the root.
    assert [n for n in names if n.endswith("manifest.json")] == ["manifest.json"]

    exclusions = json.loads(ctx.storage.get(ctx.job_relative("metadata", "zip_exclusions.json")).read_bytes())
    source_exclusion = [e for e in exclusions if e["reason"] == "source_video_excluded_by_configuration"]
    assert len(source_exclusion) == 1
    assert source_exclusion[0]["path"] == "source/video.mp4"
    assert source_exclusion[0]["still_available_at"].endswith("/video")
    # The file itself is untouched on disk.
    assert ctx.storage.exists(ctx.job_relative("source", "video.mp4"))


def test_zip_includes_source_when_explicitly_enabled(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.pipeline.steps.zip_output import ZipOutputStep

    monkeypatch.setenv("ZIP_INCLUDE_SOURCE_VIDEO", "true")
    get_settings.cache_clear()
    try:
        ctx, _, _ = make_ctx(tmp_path)
        ctx.storage.save_bytes(ctx.job_relative("manifest.json"), b"{}")
        ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"vid")
        ZipOutputStep().run(ctx)
        with zipfile.ZipFile(ctx.storage.get(ctx.job_relative("output.zip"))) as zf:
            assert "source/video.mp4" in set(zf.namelist())
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_manifest_marks_source_as_excluded_from_zip(tmp_path):
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx, _, _ = make_ctx(tmp_path, options={})
    ctx.shared.update(
        {
            "mode": "adaptive",
            "original_filename": "clip.mp4",
            "stored_source_filename": "video.mp4",
            "source_relative_path": ctx.job_relative("source", "video.mp4"),
            "started_at": "2026-01-01T00:00:00+00:00",
            "frame_count": 0,
            "frame_counts_by_category": {"adaptive": 0, "opening_dense": 0, "key_events": 0},
            "video": {"duration_seconds": 5.0, "has_audio": False},
            "transcript": {"language": None, "skipped": True, "skipped_reason": "no_audio_track"},
            "frames": [],
        }
    )
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"vid")
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.json"), b"{}")

    GenerateMetadataStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    source_entry = [f for f in manifest["files"] if f["path"].startswith("source/")][0]
    assert source_entry["included_in_zip"] is False
    assert "/video" in source_entry["reason"]
    # The manifest still describes the file, size and all.
    assert source_entry["size_bytes"] == 3
