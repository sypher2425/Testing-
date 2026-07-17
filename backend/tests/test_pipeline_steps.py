import json
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.storage.local import LocalStorageBackend
from app.utils.ffmpeg import FFmpegError, ProbeResult


def make_ctx(tmp_path, job_id="job1", options=None) -> PipelineContext:
    storage = LocalStorageBackend(tmp_path)
    logs = []
    shared_state = {"step_progress": {}}

    return PipelineContext(
        job_id=job_id,
        storage=storage,
        options=options or {},
        log=lambda level, msg: logs.append((level, msg)),
        set_step_progress=lambda step, pct: shared_state["step_progress"].__setitem__(step, pct),
        should_cancel=lambda: False,
        update_job=lambda fields: shared_state.update(fields),
    ), logs, shared_state


def test_probe_step_success(tmp_path):
    from app.pipeline.steps.probe import ProbeStep

    ctx, logs, shared_state = make_ctx(tmp_path)
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    fake_result = ProbeResult(
        duration_seconds=10.0, width=640, height=480, fps=30.0, codec="h264", has_audio=True, raw={}
    )
    with patch("app.pipeline.steps.probe.ffprobe", return_value=fake_result):
        ProbeStep().run(ctx)

    assert ctx.shared["video"]["duration_seconds"] == 10.0
    assert shared_state["has_audio"] is True


def test_probe_step_failure_raises_pipeline_error(tmp_path):
    from app.pipeline.steps.probe import ProbeStep

    ctx, _, _ = make_ctx(tmp_path)
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"not a real video")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    with patch(
        "app.pipeline.steps.probe.ffprobe",
        side_effect=FFmpegError("bad file", cmd=["ffprobe"], returncode=1, stderr="corrupt"),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            ProbeStep().run(ctx)
    assert exc_info.value.code == "probe_failed"


def test_transcribe_step_skips_when_no_audio(tmp_path):
    from app.pipeline.steps.transcribe import TranscribeStep

    ctx, logs, shared_state = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": False, "duration_seconds": 5.0}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    TranscribeStep().run(ctx)

    assert ctx.shared["transcript"]["skipped"] is True
    assert ctx.shared["transcript"]["skipped_reason"] == "no_audio_track"
    transcript_json = json.loads(ctx.storage.get(ctx.job_relative("transcript", "transcript.json")).read_bytes())
    assert transcript_json["skipped"] is True
    assert any("no audio" in m.lower() for _, m in logs)


def test_transcribe_step_writes_outputs_from_mocked_model(tmp_path):
    from app.pipeline.steps import transcribe as transcribe_module

    ctx, _, shared_state = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True, "duration_seconds": 2.5}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    class FakeSegment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class FakeInfo:
        language = "en"

    fake_segments = [FakeSegment(0.0, 1.2, "Hello there."), FakeSegment(1.2, 2.5, "General Kenobi.")]

    class FakeModel:
        def transcribe(self, *args, **kwargs):
            return fake_segments, FakeInfo()

    with patch("app.pipeline.steps.transcribe.extract_audio_wav"), patch.object(
        transcribe_module, "_get_whisper_model", return_value=FakeModel()
    ):
        transcribe_module.TranscribeStep().run(ctx)

    assert ctx.shared["language"] == "en"
    txt = ctx.storage.get(ctx.job_relative("transcript", "transcript.txt")).read_bytes().decode()
    assert "Hello there." in txt
    srt = ctx.storage.get(ctx.job_relative("transcript", "subtitles.srt")).read_bytes().decode()
    assert "-->" in srt


def test_extract_frames_every_frame_rejects_when_over_cap(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, _, _ = make_ctx(tmp_path, options={"mode": "every_frame", "frame_format": "jpeg", "frame_max_dim": 1280})
    ctx.shared["video"] = {"duration_seconds": 10_000.0, "fps": 30.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    with pytest.raises(PipelineFailedError) as exc_info:
        ExtractFramesStep().run(ctx)
    assert exc_info.value.code == "too_many_frames"


def test_extract_frames_interval_mode(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, _, shared_state = make_ctx(
        tmp_path, options={"mode": "interval", "interval_ms": 1000, "frame_format": "jpeg", "frame_max_dim": 1280}
    )
    ctx.shared["video"] = {"duration_seconds": 3.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract):
        ExtractFramesStep().run(ctx)

    assert ctx.shared["frame_count"] == 3
    assert shared_state["frame_count"] == 3
    frames = ctx.shared["frames"]
    assert frames[0]["image"] == "0000.000.jpg"
    assert frames[1]["image"] == "0001.000.jpg"


def test_extract_frames_adaptive_mode_uses_scene_detection(tmp_path):
    from app.pipeline.steps import extract_frames as extract_frames_module

    ctx, _, shared_state = make_ctx(
        tmp_path, options={"mode": "adaptive", "target_frames": 40, "frame_format": "jpeg", "frame_max_dim": 1280}
    )
    ctx.shared["video"] = {"duration_seconds": 60.0, "fps": 30.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    # 5 fake scenes spanning the 60s video; well below ADAPTIVE_MIN_FRAMES (30),
    # so the step must fall back to interval sampling to reach the minimum.
    fake_scenes = [(i * 12.0, (i + 1) * 12.0, float(i)) for i in range(5)]

    def fake_extract(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch.object(extract_frames_module, "_detect_scenes", return_value=fake_scenes), patch(
        "app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract
    ):
        extract_frames_module.ExtractFramesStep().run(ctx)

    assert ctx.shared["frame_count"] >= 30
    assert all(f["mode"] == "adaptive" for f in ctx.shared["frames"])
    assert all("scene_id" in f for f in ctx.shared["frames"])


def test_generate_metadata_step_writes_manifest(tmp_path):
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx, _, _ = make_ctx(tmp_path, options={})
    ctx.shared["mode"] = "adaptive"
    ctx.shared["original_filename"] = "clip.mp4"
    ctx.shared["stored_source_filename"] = "video.mp4"
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    ctx.shared["started_at"] = "2024-01-01T00:00:00+00:00"
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["video"] = {"duration_seconds": 5.0, "width": 640, "height": 480, "fps": 30.0, "codec": "h264", "has_audio": True}
    ctx.shared["transcript"] = {"language": "en", "skipped": False, "segments": []}
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.txt"), b"")
    ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.json"), b"{}")
    ctx.storage.save_bytes(ctx.job_relative("transcript", "subtitles.srt"), b"")
    ctx.shared["frames"] = [{"frame": 0, "timestamp": 0.0, "image": "0000.000.jpg", "mode": "adaptive", "scene_id": 0}]
    ctx.storage.save_bytes(ctx.job_relative("frames", "0000.000.jpg"), b"jpg")

    GenerateMetadataStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    assert manifest["job_id"] == ctx.job_id
    assert manifest["frame_count"] == 1
    assert manifest["transcript_available"] is True
    paths = {f["path"] for f in manifest["files"]}
    assert "source/video.mp4" in paths
    assert "frames/0000.000.jpg" in paths
    assert "metadata/frames.json" in paths


def test_zip_output_step_creates_archive_with_all_files(tmp_path):
    from app.pipeline.steps.zip_output import ZipOutputStep
    import zipfile

    ctx, _, _ = make_ctx(tmp_path)
    ctx.storage.save_bytes(ctx.job_relative("manifest.json"), b"{}")
    ctx.storage.save_bytes(ctx.job_relative("frames", "0000.000.jpg"), b"jpg")

    ZipOutputStep().run(ctx)

    zip_path = ctx.storage.get(ctx.job_relative("output.zip"))
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert "frames/0000.000.jpg" in names
