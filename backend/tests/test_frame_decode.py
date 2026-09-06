"""Real codec/PTS checks plus bounded sampling and cancellation regressions."""
import math
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from app.pipeline.context import JobCancelled
from app.pipeline.errors import PipelineFailedError
from app.pipeline.steps.extract_frames import ExtractFramesStep, Selection, _select_adaptive_timestamps, resolve_interval
from app.utils.ffmpeg import FFmpegError, extract_frame_at, ffprobe
from app.utils.frame_decode import DecodedFrame, TimestampCollector, decode_frames, run_media_process
from tests.test_pipeline_steps import make_ctx


@pytest.fixture
def clip(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=30:duration=3", "-c:v", "libx264", str(path)],
                   check=True, capture_output=True)
    return path


def test_dense_decode_retains_original_pts_and_excludes_range_end(clip, tmp_path):
    frames = decode_frames(str(clip), tmp_path / "frames", start=1, end=2,
                           interval=0.2, max_frames=100, hwaccel="none")
    assert [f.timestamp for f in frames] == pytest.approx([1, 1.2, 1.4, 1.6, 1.8])
    assert all(f.path.is_file() for f in frames)


def test_sparse_seek_reports_actual_frame_instead_of_requested_time(clip, tmp_path):
    actual = extract_frame_at(str(clip), str(tmp_path / "frame.jpg"), 1.017)
    assert actual == pytest.approx(31 / 30)
    assert actual != 1.017


def test_timestamp_parser_keeps_subframe_precision_in_long_video():
    collect = TimestampCollector(1)
    collect("[Parsed_showinfo_0] config in time_base: 1/90000, frame_rate: 60/1")
    collect("[Parsed_showinfo_0] n: 0 pts: 162001500 pts_time:1800.02 fmt:yuv420p")
    assert collect.timestamps == pytest.approx([1800 + 1 / 60])


def test_requests_faster_than_source_do_not_duplicate_frames(clip, tmp_path):
    frames = decode_frames(str(clip), tmp_path / "frames", start=0, end=1,
                           interval=1 / 60, max_frames=100, hwaccel="none")
    assert len(frames) == 30
    assert len({f.timestamp for f in frames}) == 30


def test_real_every_frame_honors_vfr_pts(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "vfr.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=10:duration=2", "-vf", "select='lt(n,4)+gt(n,9)'",
                    "-fps_mode", "vfr", "-c:v", "libx264", str(source)], check=True, capture_output=True)
    frames = decode_frames(str(source), tmp_path / "frames", start=0, end=2,
                           max_frames=100, hwaccel="none")
    expected = [0, .1, .2, .3] + [i / 10 for i in range(10, 20)]
    assert [f.timestamp for f in frames] == pytest.approx(expected)


def test_container_start_offset_is_normalized(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "offset.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=10:duration=2", "-c:v", "libx264",
                    "-output_ts_offset", "5", str(source)], check=True, capture_output=True)
    frames = decode_frames(str(source), tmp_path / "frames", start=.5, end=1,
                           interval=.2, max_frames=10, hwaccel="none")
    assert [f.timestamp for f in frames] == pytest.approx([.5, .7, .9])
    assert ffprobe(str(source)).duration_seconds == pytest.approx(2.0)


@pytest.mark.parametrize("fail_hardware", [False, True])
def test_ffmpeg_decodes_av1_without_opencv_video_capture(tmp_path, fail_hardware):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    source = tmp_path / "av1.mkv"
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                             "-i", "testsrc2=size=128x72:rate=8:duration=1", "-c:v", "libaom-av1",
                             "-cpu-used", "8", "-crf", "50", str(source)], capture_output=True)
    if result.returncode:
        pytest.skip("test FFmpeg does not include libaom-av1 encoder")
    stats = {}
    attempts = []
    from app.utils import frame_decode as module
    module._HARDWARE_FAILURE_UNTIL.clear()
    def run(cmd, **kwargs):
        attempts.append(cmd)
        if fail_hardware and len(attempts) == 1:
            # Simulate a driver failing after a partial frame. The fallback
            # must discard both that JPEG and its unrelated timestamp.
            (tmp_path / "frames" / "decoded-00000099.jpg").write_bytes(b"partial")
            kwargs["on_line"]("n: 0 pts: 999 pts_time:999")
            raise FFmpegError("GPU unavailable", cmd=cmd, returncode=1, stderr="driver unavailable")
        return run_media_process(cmd, **kwargs)
    with patch("cv2.VideoCapture", side_effect=AssertionError("AV1 must use FFmpeg")), patch.object(
        module, "_available_nvidia_decoders", return_value=frozenset({"av1_cuvid"})
    ), patch.object(module, "run_media_process", side_effect=run):
        frames = decode_frames(str(source), tmp_path / "frames", start=0, end=1,
                               interval=.25, max_frames=10, hwaccel="auto" if fail_hardware else "none",
                               source_codec="av1", decode_stats=stats)
    assert len(frames) == 4
    assert [frame.timestamp for frame in frames] == pytest.approx([0, .25, .5, .75])
    assert stats["successful_decode_operations"] == {"software_default": 1}
    if fail_hardware:
        assert len(attempts) == 2
        assert "av1_cuvid" in attempts[0] and "av1_cuvid" not in attempts[1]
        assert stats["hardware_fallbacks"] == 1
        assert not (tmp_path / "frames" / "decoded-00000099.jpg").exists()
    module._HARDWARE_FAILURE_UNTIL.clear()


def test_decoder_discovery_is_cached_and_auto_selects_explicit_av1():
    from app.utils import frame_decode as module
    module._available_nvidia_decoders.cache_clear()
    module._HARDWARE_FAILURE_UNTIL.clear()
    output = subprocess.CompletedProcess([], 0, b" V..... av1_cuvid NVIDIA CUVID AV1 decoder\n", b"")
    try:
        with patch.object(module.subprocess, "run", return_value=output) as discover:
            assert module._decoder_options("unused", "auto", "av1") == (["-c:v", "av1_cuvid"], "av1_cuvid")
            assert module._decoder_options("unused", "auto", "av1")[1] == "av1_cuvid"
            assert discover.call_count == 1
            assert module._decoder_options("unused", "none", "av1") == ([], "software_default")
            module._HARDWARE_FAILURE_UNTIL["av1_cuvid"] = time.monotonic() + 60
            assert module._decoder_options("unused", "auto", "av1") == ([], "software_default")
    finally:
        module._available_nvidia_decoders.cache_clear()
        module._HARDWARE_FAILURE_UNTIL.clear()


def test_keyframe_only_prescan_bypasses_cuvid_to_keep_actual_keyframes(clip, tmp_path):
    from app.utils import frame_decode as module
    stats = {}
    with patch.object(module, "_available_nvidia_decoders", return_value=frozenset({"h264_cuvid"})), patch.object(
        module, "run_media_process", wraps=run_media_process
    ) as run:
        frames = decode_frames(str(clip), tmp_path / "frames", start=0, end=3,
                               interval=.2, max_frames=30, keyframes_only=True,
                               source_codec="h264", hwaccel="auto", decode_stats=stats)
    # This fixture has one keyframe. CUVID silently returns the whole grid
    # when asked to skip non-keyframes, defeating Fast mode's promise.
    assert [frame.timestamp for frame in frames] == [0]
    assert "h264_cuvid" not in run.call_args.args[0]
    assert stats["requested_hwaccel"] == "auto"
    assert stats["successful_decode_operations"] == {"software_default": 1}
    assert stats["hardware_bypasses"] == {"keyframes_only": 1}
    assert stats.get("hardware_fallbacks", 0) == 0


@pytest.mark.parametrize("failure", [JobCancelled("cancelled"), FFmpegError("cannot decode", cmd=[], returncode=1, stderr="")])
def test_hardware_retry_never_swallows_cancellation_or_retries_cpu_twice(tmp_path, failure):
    from app.utils import frame_decode as module
    module._HARDWARE_FAILURE_UNTIL.clear()
    try:
        with patch.object(module, "_available_nvidia_decoders", return_value=frozenset({"av1_cuvid"})), patch.object(
            module, "run_media_process", side_effect=failure
        ) as run:
            with pytest.raises(type(failure)):
                decode_frames("unused", tmp_path / "frames", start=0, end=1, max_frames=1,
                              source_codec="av1", hwaccel="auto")
            assert run.call_count == (1 if isinstance(failure, JobCancelled) else 2)
    finally:
        module._HARDWARE_FAILURE_UNTIL.clear()


def test_cancellation_interrupts_and_reaps_running_decoder():
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    def cancelled():
        raise JobCancelled("cancelled")

    started = time.monotonic()
    with patch("app.utils.frame_decode.subprocess.Popen", side_effect=launch):
        with pytest.raises(JobCancelled):
            run_media_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=10, check_cancel=cancelled)
    assert time.monotonic() - started < 3
    assert processes[0].poll() is not None


def test_timeout_interrupts_running_decoder():
    started = time.monotonic()
    with pytest.raises(FFmpegError, match="timed out"):
        run_media_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.15)
    assert time.monotonic() - started < 3


def test_adaptive_target_is_honored_even_when_scene_count_differs():
    scenes = [(i * 15, i * 15 + 10, float(i)) for i in range(120)]
    for target in (30, 80, 300):
        selections = _select_adaptive_timestamps(1800, scenes, target, 30, 2000)
        assert len(selections) == target
        # Coverage remains present in all six five-minute blocks even when
        # the largest change scores are concentrated at the end.
        assert {min(5, int(s.timestamp // 300)) for s in selections} == set(range(6))


def test_frame_range_and_budget_keep_original_timeline(tmp_path):
    from app.config import get_settings
    ctx, _, _ = make_ctx(tmp_path, options={"mode": "interval", "interval_ms": 17,
                                           "range_start_seconds": 100, "range_end_seconds": 130,
                                           "frame_budget": 30})
    selections = ExtractFramesStep()._select("interval", 1800, 30, ctx, get_settings())
    assert len(selections) == 30
    assert selections[0].timestamp == 100
    assert selections[-1].timestamp == 129
    assert ctx.shared["interval_config"]["effective_interval_seconds"] == 1


def test_burst_budget_keeps_coverage_and_reports_reduction(tmp_path):
    ctx, _, _ = make_ctx(tmp_path, options={"frame_bursts": [{"start_seconds": 50, "end_seconds": 60, "fps": 60}]})
    base = [Selection(float(t), None) for t in range(100)]
    result = ExtractFramesStep()._add_bursts(ctx, base, 0, 100, 100)
    assert len(result) == 100
    assert result[0].timestamp == 0
    assert result[-1].timestamp == 99
    assert sum(50 <= sel.timestamp < 60 for sel in result) > 40
    assert ctx.shared["burst_config"]["budget_limited"] is True


def test_interval_budget_does_not_count_eof():
    plan = resolve_interval(400, .2, 2000)
    assert plan["frame_count"] == 2000
    assert plan["widened"] is False


def test_pipeline_batches_dense_requests_and_reuses_opening(clip, tmp_path):
    ctx, _, _ = make_ctx(tmp_path / "store", options={"mode": "interval", "interval_ms": 250,
                                                     "frame_budget": 100, "frame_max_dim": 160})
    ctx.shared["video"] = {"duration_seconds": 3, "fps": 30}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), clip.read_bytes())
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    from app.pipeline.steps import extract_frames as module
    with patch.object(module, "decode_frames", wraps=decode_frames) as batch, patch.object(
        module, "extract_frame_at", wraps=extract_frame_at
    ) as sparse:
        ExtractFramesStep().run(ctx)
    assert batch.call_count == 1
    assert sparse.call_count == 0
    assert ctx.shared["frame_count"] == 12
    assert ctx.shared["frame_counts_by_category"]["opening_dense"] == 12
    assert all(frame["timestamp_source"] == "decoded_pts" for frame in ctx.shared["frames"])


def test_exhaustive_range_fails_if_actual_vfr_count_exceeds_budget(clip, tmp_path):
    ctx, _, _ = make_ctx(tmp_path / "store", options={"mode": "every_frame", "range_start_seconds": 1,
                                                     "range_end_seconds": 2, "frame_budget": 10})
    # Deliberately misleading average rate: the decoder's actual output
    # count still enforces the budget for a selected subrange.
    ctx.shared["video"] = {"duration_seconds": 3, "fps": 1}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), clip.read_bytes())
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    with pytest.raises(PipelineFailedError) as exc:
        ExtractFramesStep().run(ctx)
    assert exc.value.code == "too_many_frames"
