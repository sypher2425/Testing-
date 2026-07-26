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


def test_extract_frames_skips_one_bad_frame_instead_of_failing_job(tmp_path):
    """A single frame that fails all retry attempts should be skipped, not
    take down the whole job — this is the 'safety measure' regression test
    for the "Failed to extract frame at Xs" bug."""
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, logs, shared_state = make_ctx(
        tmp_path, options={"mode": "interval", "interval_ms": 1000, "frame_format": "jpeg", "frame_max_dim": 1280}
    )
    ctx.shared["video"] = {"duration_seconds": 4.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    # Selections land at 0.0, 1.0, 2.0, 3.0 — make every attempt for t=2.0 fail
    # (all 3 retries), while every other timestamp succeeds on the first try.
    def fake_extract(source, output_path, timestamp, **kwargs):
        if abs(timestamp - 2.0) < 0.3:
            raise FFmpegError("boom", cmd=["ffmpeg"], returncode=1, stderr="no frame")
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch("app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_extract):
        ExtractFramesStep().run(ctx)

    # 4 selected, 1 unrecoverable -> 3 survive, job did not raise.
    assert ctx.shared["frame_count"] == 3
    assert shared_state["frame_count"] == 3
    assert any("skipped" in msg.lower() or "skipping" in msg.lower() for _, msg in logs)
    # Remaining adaptive frames are renumbered contiguously.
    adaptive = [f for f in ctx.shared["frames"] if f["category"] == "adaptive"]
    assert [f["frame"] for f in adaptive] == [0, 1, 2]


def test_extract_frames_raises_when_every_frame_fails(tmp_path):
    from app.pipeline.steps.extract_frames import ExtractFramesStep

    ctx, _, _ = make_ctx(
        tmp_path, options={"mode": "interval", "interval_ms": 1000, "frame_format": "jpeg", "frame_max_dim": 1280}
    )
    ctx.shared["video"] = {"duration_seconds": 2.0, "fps": 25.0, "has_audio": False}
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    with patch(
        "app.pipeline.steps.extract_frames.extract_frame_at",
        side_effect=FFmpegError("boom", cmd=["ffmpeg"], returncode=1, stderr="no frame"),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            ExtractFramesStep().run(ctx)
    assert exc_info.value.code == "frame_extraction_failed"


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
    adaptive = [f for f in ctx.shared["frames"] if f["category"] == "adaptive"]
    assert all(f["mode"] == "adaptive" for f in adaptive)
    assert all("scene_id" in f for f in adaptive)
    # v2: dense opening frames are extracted alongside (default 8s / 0.25s).
    dense = [f for f in ctx.shared["frames"] if f["category"] == "opening_dense"]
    assert len(dense) == 32
    assert all(f["image"].startswith("opening_dense/") for f in dense)


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


def _fake_metadata(**overrides):
    from app.utils.ytdlp import VideoMetadata

    defaults = dict(
        source_url="https://youtu.be/xyz",
        platform="youtube",
        title="Original Title",
        description="a video",
        uploader="uploader1",
        uploader_id="uploader1",
        upload_date="2024-01-15",
        view_count=1000,
        like_count=50,
        comment_count=3,
        share_count=None,
        hashtags=["fun"],
        duration=12.0,
        ext="mp4",
        filesize_approx=1000,
    )
    defaults.update(overrides)
    return VideoMetadata(**defaults)


def test_fetch_source_step_noop_for_plain_upload(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep

    ctx, _, shared_state = make_ctx(tmp_path, options={})
    ctx.shared["source_url"] = None

    FetchSourceStep().run(ctx)

    assert "performance" not in ctx.shared
    assert shared_state["step_progress"]["fetching_source"] == 100


def test_fetch_source_step_manual_only_builds_performance(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep

    ctx, _, shared_state = make_ctx(
        tmp_path,
        options={"performance_overrides": {"title": "My Manual Title", "view_count": 42, "hashtags": ["x"]}},
    )
    ctx.shared["source_url"] = None

    FetchSourceStep().run(ctx)

    perf = ctx.shared["performance"]
    assert perf["platform"] == "manual"
    assert perf["title"] == "My Manual Title"
    assert perf["view_count"] == 42
    assert perf["fields_from"]["title"] == "manual"
    assert shared_state["performance"] == perf


def test_fetch_source_step_url_success_merges_manual_override(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep

    ctx, logs, shared_state = make_ctx(
        tmp_path,
        options={"performance_overrides": {"title": "Overridden Title"}},
    )
    ctx.shared["source_url"] = "https://youtu.be/xyz"
    ctx.shared["original_filename"] = "https://youtu.be/xyz"

    def fake_download(url, dest_dir, *, log):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "video.mp4"
        path.write_bytes(b"fake video bytes")
        return path

    fake_extraction = {
        "status": "success",
        "reason": None,
        "error": None,
        "platform_comment_count": 3,
        "extracted_comment_count": 1,
        "attempted_at": "2026-07-18T00:00:00+00:00",
        "comments": [
            {
                "id": "c1",
                "text": "hi",
                "author": "a",
                "author_id": None,
                "like_count": 1,
                "reply_count": None,
                "timestamp": None,
                "is_pinned": None,
                "author_is_uploader": None,
            }
        ],
    }
    with patch("app.pipeline.steps.fetch_source.extract_metadata", return_value=_fake_metadata()), patch(
        "app.pipeline.steps.fetch_source.download_video", side_effect=fake_download
    ), patch("app.pipeline.steps.fetch_source.extract_comments", return_value=fake_extraction):
        FetchSourceStep().run(ctx)

    assert ctx.shared["stored_source_filename"] == "video.mp4"
    assert ctx.storage.exists(ctx.shared["source_relative_path"])
    assert ctx.shared["source_sha256"] == shared_state["source_sha256"]
    assert len(shared_state["source_sha256"]) == 64

    perf = ctx.shared["performance"]
    assert perf["title"] == "Overridden Title"  # manual wins
    assert perf["fields_from"]["title"] == "manual"
    assert perf["view_count"] == 1000  # auto value, no manual override given
    assert perf["fields_from"]["view_count"] == "auto"
    assert perf["fields_status"]["view_count"] == {"status": "success", "source": "auto", "precision": "exact"}
    assert perf["platform"] == "youtube"

    # original_filename should be updated to the manual title override.
    assert shared_state["original_filename"] == "Overridden Title"

    # Legacy v1 file keeps the old simple shape.
    legacy = json.loads(ctx.storage.get(ctx.job_relative("performance", "comments.json")).read_bytes())
    assert legacy == [{"author": "a", "text": "hi", "like_count": 1, "timestamp": None}]
    # v2 files carry the full comments + explicit extraction outcome.
    top = json.loads(ctx.storage.get(ctx.job_relative("comments", "top_comments.json")).read_bytes())
    assert top[0]["id"] == "c1"
    extraction_status = json.loads(
        ctx.storage.get(ctx.job_relative("comments", "extraction_status.json")).read_bytes()
    )
    assert extraction_status["status"] == "success"
    assert "comments" not in extraction_status


def test_fetch_source_step_backfills_instagram_view_count_from_grid_fallback(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep

    ctx, _, _ = make_ctx(tmp_path, options={"performance_overrides": {}})
    ctx.shared["source_url"] = "https://instagram.com/reel/abc123"
    ctx.shared["original_filename"] = "https://instagram.com/reel/abc123"

    ig_metadata = _fake_metadata(
        platform="instagram",
        view_count=None,
        uploader="Some Account",
        uploader_id="someaccount",
        raw={"id": "abc123"},
    )

    def fake_download(url, dest_dir, *, log):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "video.mp4"
        path.write_bytes(b"fake video bytes")
        return path

    empty_extraction = {
        "status": "extraction_failed",
        "reason": "zero_results_unexpected",
        "error": "none returned",
        "platform_comment_count": 5,
        "extracted_comment_count": 0,
        "attempted_at": "2026-07-18T00:00:00+00:00",
        "comments": [],
    }
    with patch("app.pipeline.steps.fetch_source.extract_metadata", return_value=ig_metadata), patch(
        "app.pipeline.steps.fetch_source.download_video", side_effect=fake_download
    ), patch("app.pipeline.steps.fetch_source.extract_comments", return_value=empty_extraction), patch(
        "app.pipeline.steps.fetch_source.fetch_profile_reel_view_count", return_value=7929
    ) as mock_fallback:
        FetchSourceStep().run(ctx)

    mock_fallback.assert_called_once_with("someaccount", "abc123", log=ctx.log)
    perf = ctx.shared["performance"]
    assert perf["view_count"] == 7929
    assert perf["fields_from"]["view_count"] == "auto"
    assert perf["fields_status"]["view_count"]["status"] == "success"


def test_fetch_source_step_video_unavailable_fails_job(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep
    from app.utils.ytdlp import YtDlpError

    ctx, _, _ = make_ctx(tmp_path, options={})
    ctx.shared["source_url"] = "https://youtu.be/private"

    with patch(
        "app.pipeline.steps.fetch_source.extract_metadata",
        side_effect=YtDlpError("video_unavailable", "Private video"),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            FetchSourceStep().run(ctx)

    assert exc_info.value.code == "video_unavailable"


def test_fetch_source_step_download_failure_fails_job(tmp_path):
    from app.pipeline.steps.fetch_source import FetchSourceStep
    from app.utils.ytdlp import YtDlpError

    ctx, _, _ = make_ctx(tmp_path, options={})
    ctx.shared["source_url"] = "https://youtu.be/xyz"

    with patch("app.pipeline.steps.fetch_source.extract_metadata", return_value=_fake_metadata(filesize_approx=None)), patch(
        "app.pipeline.steps.fetch_source.download_video",
        side_effect=YtDlpError("extractor_outdated", "still broken after update"),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            FetchSourceStep().run(ctx)

    assert exc_info.value.code == "extractor_outdated"
