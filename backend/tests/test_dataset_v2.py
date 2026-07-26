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
    ctx.shared["stage_reports"] = [
        {"stage": "probing", "status": "success", "started_at": "x", "completed_at": "y", "error": None}
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
    assert manifest["dataset_schema_version"] == "2.0"
    assert manifest["source_video_sha256"] == "ab" * 32
    assert manifest["frame_counts"]["adaptive"] == 1
    assert manifest["extraction_report"][0]["stage"] == "probing"
    assert manifest["posting_context"]["platform"]["value"] == "youtube"
    assert manifest["analysis_summary"]["object_first_visible_seconds"]["value"] == 1.8
    assert manifest["events_count"] == 1
    # v1 keys still present and unchanged in meaning.
    assert manifest["frame_count"] == 1
    assert manifest["performance"]["view_count"] == 100
    assert manifest["analyses"] == {}

    # Files written alongside the manifest.
    assert ctx.storage.exists(ctx.job_relative("content", "caption.txt"))
    assert ctx.storage.exists(ctx.job_relative("content", "audio.json"))
    assert ctx.storage.exists(ctx.job_relative("analytics", "performance.json"))
    events = json.loads(ctx.storage.get(ctx.job_relative("events.json")).read_bytes())
    assert events[0]["type"] == "object_first_visible"

    engagement = json.loads(ctx.storage.get(ctx.job_relative("analytics", "performance.json")).read_bytes())
    assert engagement["rates"]["like_rate"]["value"] == 0.05


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
