"""Dense interval sampling (the 'every 0.2s' feature).

The old behaviour capped frame count by truncating from the FRONT of the
video: a frame every 0.2s on a 60-minute video yielded 2000 frames covering
only the first 6m40s, silently, while the manifest still claimed 0.2s
sampling. These tests pin the replacement: widen the interval, cover the
whole video, and say so.
"""
from unittest.mock import patch

import pytest

from app.pipeline.steps.extract_frames import (
    _select_interval_timestamps,
    resolve_interval,
)


# ------------------------------------------------------------ resolve_interval


def test_short_video_at_0_2s_is_not_widened():
    """A 30s clip at 0.2s is 151 frames — well inside the cap."""
    plan = resolve_interval(duration=30.0, requested_interval=0.2, cap=2000)
    assert plan["widened"] is False
    assert plan["effective_interval_seconds"] == 0.2
    assert plan["reason"] is None
    assert plan["frame_count"] == 151


def test_exactly_at_the_cap_is_not_widened():
    # 2000 frames at 0.2s covers 399.8s; +1 for t=0 lands exactly on the cap.
    plan = resolve_interval(duration=399.8, requested_interval=0.2, cap=2000)
    assert plan["widened"] is False


def test_long_video_at_0_2s_widens_instead_of_truncating():
    """The headline case: an hour of video asked for at 0.2s."""
    plan = resolve_interval(duration=3600.0, requested_interval=0.2, cap=2000)
    assert plan["widened"] is True
    assert plan["requested_interval_seconds"] == 0.2
    assert plan["effective_interval_seconds"] == pytest.approx(1.8, abs=0.001)
    assert plan["frame_count"] == 2000
    # The reason has to be usable by a human reading the job log.
    assert "0.2s" in plan["reason"]
    assert "2000" in plan["reason"]
    assert "1.800s" in plan["reason"]


def test_widened_interval_still_covers_the_whole_video():
    """This is the property the old code violated."""
    duration = 3600.0
    selections = _select_interval_timestamps(duration, 0.2, 2000)
    assert len(selections) <= 2000
    last = selections[-1].timestamp
    # The final sample must be near the end, not at 400s.
    assert last > duration * 0.99, f"coverage stopped at {last}s of {duration}s"
    assert selections[0].timestamp == 0.0


def test_old_front_truncation_bug_is_gone():
    """Explicit regression: 2000 frames at a literal 0.2s spacing would end at
    399.8s. The fix must NOT produce that."""
    selections = _select_interval_timestamps(3600.0, 0.2, 2000)
    assert selections[-1].timestamp > 3000, (
        "still truncating from the front — coverage ends early"
    )


def test_timestamps_are_strictly_increasing_and_unique():
    selections = _select_interval_timestamps(600.0, 0.2, 2000)
    stamps = [s.timestamp for s in selections]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_no_float_drift_over_thousands_of_frames():
    """Repeated `t += interval` accumulates error; i*interval does not."""
    selections = _select_interval_timestamps(400.0, 0.2, 2000)
    for i, sel in enumerate(selections):
        assert sel.timestamp == pytest.approx(i * 0.2, abs=0.002)


def test_no_timestamp_ever_exceeds_duration():
    for duration, interval in ((30.0, 0.2), (3600.0, 0.2), (7.3, 1.0), (0.5, 0.2)):
        for sel in _select_interval_timestamps(duration, interval, 2000):
            assert sel.timestamp < duration or duration == 0


def test_degenerate_inputs_return_a_single_frame():
    assert len(_select_interval_timestamps(0.0, 0.2, 2000)) == 1
    assert _select_interval_timestamps(0.0, 0.2, 2000)[0].timestamp == 0.0
    # A video shorter than one interval still yields the opening frame.
    assert len(_select_interval_timestamps(0.1, 1.0, 2000)) == 1


def test_no_cap_means_no_widening():
    plan = resolve_interval(duration=3600.0, requested_interval=0.2, cap=None)
    assert plan["widened"] is False
    assert plan["effective_interval_seconds"] == 0.2


def test_per_second_mode_unchanged_for_normal_videos():
    plan = resolve_interval(duration=120.0, requested_interval=1.0, cap=2000)
    assert plan["widened"] is False
    assert plan["frame_count"] == 121


# --------------------------------------------------------- step-level wiring


def _ctx_for_interval(tmp_path, interval_ms: int, duration: float):
    from tests.test_pipeline_steps import make_ctx

    ctx, logs, _ = make_ctx(
        tmp_path,
        options={
            "mode": "interval",
            "interval_ms": interval_ms,
            "frame_format": "jpeg",
            "frame_max_dim": 1280,
            "opening_dense_enabled": False,
        },
    )
    ctx.shared["video"] = {"duration_seconds": duration, "fps": 30.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    return ctx, logs


def test_step_records_interval_config_and_warns_when_widened(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, logs = _ctx_for_interval(tmp_path, interval_ms=200, duration=3600.0)

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    # Only the selection maths matters here; cap the work by patching MAX_FRAMES.
    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract), patch(
        "app.pipeline.steps.extract_frames.get_settings"
    ) as fake_settings:
        s = fake_settings.return_value
        s.MAX_FRAMES = 20
        s.FRAME_MAX_DIM_DEFAULT = 1280
        s.FRAME_JPEG_QUALITY = 85
        s.FFMPEG_TIMEOUT_SECONDS = 60
        s.HEARTBEAT_INTERVAL_SECONDS = 10
        s.OPENING_DENSE_DURATION = 8.0
        s.OPENING_DENSE_INTERVAL = 0.25
        ExtractFramesStep().run(ctx)

    plan = ctx.shared["interval_config"]
    assert plan["widened"] is True
    assert plan["requested_interval_seconds"] == 0.2
    assert plan["effective_interval_seconds"] == pytest.approx(180.0)
    assert any("Widened" in msg for level, msg in logs if level == "warning")

    # Coverage really does span the video.
    stamps = [f["timestamp"] for f in ctx.shared["frames"]]
    assert max(stamps) > 3000


def test_step_does_not_warn_when_interval_fits(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, logs = _ctx_for_interval(tmp_path, interval_ms=1000, duration=5.0)

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract):
        ExtractFramesStep().run(ctx)

    plan = ctx.shared["interval_config"]
    assert plan["widened"] is False
    assert not any("Widened" in msg for level, msg in logs if level == "warning")


def test_manifest_records_effective_and_requested_interval(tmp_path):
    """A consumer must never mistake the widened interval for what was asked."""
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep
    import json

    from tests.test_pipeline_steps import make_ctx

    ctx, _, _ = make_ctx(tmp_path, options={"mode": "interval", "interval_ms": 200})
    ctx.shared.update(
        {
            "mode": "interval",
            "original_filename": "clip.mp4",
            "stored_source_filename": "video.mp4",
            "source_relative_path": ctx.job_relative("source", "video.mp4"),
            "started_at": "2026-01-01T00:00:00+00:00",
            "frame_count": 0,
            "frame_counts_by_category": {"adaptive": 0, "opening_dense": 0, "key_events": 0},
            "video": {"duration_seconds": 3600.0, "has_audio": False},
            "transcript": {"language": None, "skipped": True, "skipped_reason": "no_audio_track"},
            "frames": [],
            "interval_config": {
                "requested_interval_seconds": 0.2,
                "effective_interval_seconds": 1.8,
                "widened": True,
                "reason": "widened for coverage",
                "frame_count": 2000,
            },
        }
    )
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"vid")
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.json"), b"{}")

    GenerateMetadataStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    params = manifest["extraction_params"]
    assert params["interval"]["widened"] is True
    assert params["interval_ms"] == 1800  # effective
    assert params["requested_interval_ms"] == 200  # what the user asked for
