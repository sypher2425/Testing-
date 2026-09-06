"""Integration contracts for accelerated transcription, options and reports."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.config import get_settings
from app.main import app
from app.models import Job
from app.pipeline.context import JobCancelled
from app.pipeline.steps.load_model import LoadWhisperModelStep
from app.pipeline.steps.transcribe import TranscribeStep
from app.schemas import CreateJobOptions
from app.utils.transcript_cache import prepare_transcript, read_cached_transcript, write_cached_transcript
from tests.test_pipeline_steps import make_ctx


def _ctx(tmp_path, monkeypatch, **options):
    settings = get_settings()
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "ENABLE_DIARIZATION", False)
    monkeypatch.setattr(settings, "WHISPER_DEVICE", "cpu")
    ctx, _, _ = make_ctx(tmp_path / "jobs", options=options)
    ctx.shared.update({"source_sha256": "a" * 64,
                       "source_relative_path": ctx.job_relative("source", "video.mp4"),
                       "video": {"duration_seconds": 20.0, "has_audio": True}})
    return ctx


def _transcript():
    return {"language": "en", "duration": 20, "skipped": False,
            "segments": [{"start": 1, "end": 3, "text": "The existing transcript"}]}


def test_prepared_captions_skip_model_loading_and_audio_transcription(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    caption = {**_transcript(), "source": "platform_captions"}
    ctx.shared["caption_transcript"] = caption
    with patch("app.pipeline.steps.load_model.load_whisper_model") as load, \
         patch("app.pipeline.steps.transcribe.extract_audio_wav") as extract:
        LoadWhisperModelStep().run(ctx)
        TranscribeStep().run(ctx)
    load.assert_not_called()
    extract.assert_not_called()
    assert ctx.shared["transcript"]["source"] == "platform_captions"
    output = ctx.storage.get(ctx.job_relative("transcript", "transcript.json"))
    assert json.loads(output.read_text())["segments"] == caption["segments"]


def test_dataset_caption_lookup_happens_once_before_model_load(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    ctx.shared.update({"source_url": "https://www.youtube.com/watch?v=example", "caption_metadata": object()})
    def captions(context, url, metadata, language):
        context.shared["caption_transcript"] = _transcript()
        return True
    with patch("app.pipeline.steps.transcript.TranscriptSourceStep._try_captions", side_effect=captions) as lookup, \
         patch("app.pipeline.steps.load_model.load_whisper_model") as load:
        LoadWhisperModelStep().run(ctx)
        TranscribeStep().run(ctx)
    assert lookup.call_count == 1
    load.assert_not_called()


def test_cached_transcript_reused_when_only_frame_settings_change(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, processing_profile="balanced", frame_budget=100)
    write_cached_transcript(ctx, _transcript())
    ctx.options.update({"frame_budget": 9000, "interval_ms": 50, "frame_max_dim": 2560})
    with patch("app.pipeline.steps.load_model.load_whisper_model") as load, \
         patch("app.pipeline.steps.transcribe.extract_audio_wav") as extract:
        LoadWhisperModelStep().run(ctx)
        TranscribeStep().run(ctx)
    load.assert_not_called()
    extract.assert_not_called()
    assert ctx.shared["transcript"]["cache_hit"] is True


@pytest.mark.parametrize("change", ["profile_fast", "profile_detailed", "model", "language", "source", "preference"])
def test_transcript_cache_invalidates_inference_changes(tmp_path, monkeypatch, change):
    ctx = _ctx(tmp_path, monkeypatch, processing_profile="balanced")
    write_cached_transcript(ctx, _transcript())
    assert read_cached_transcript(ctx) is not None
    if change.startswith("profile_"):
        ctx.options["processing_profile"] = change.removeprefix("profile_")
    elif change == "model":
        monkeypatch.setattr(get_settings(), "WHISPER_MODEL_SIZE", "different-model")
    elif change == "language":
        ctx.options["transcript"] = {"language": "fr"}
    elif change == "source":
        ctx.shared["source_sha256"] = "b" * 64
    else:
        ctx.options["source_preference"] = "whisper_only"
    assert read_cached_transcript(ctx) is None
    assert prepare_transcript(ctx) is False


@pytest.mark.parametrize("profile,word_timestamps,beam_size", [("fast", False, 1), ("balanced", False, 5), ("detailed", True, 5)])
def test_transcription_profile_preserves_requested_word_detail(tmp_path, monkeypatch, profile, word_timestamps, beam_size):
    ctx = _ctx(tmp_path, monkeypatch, processing_profile=profile)
    word = SimpleNamespace(start=1.12345, end=1.6, word="Hello", probability=.97)
    segment = SimpleNamespace(start=1.1, end=2, text=" Hello ", words=[word], avg_logprob=-.2, no_speech_prob=.01)
    model = Mock()
    model.transcribe.return_value = (iter([segment]), SimpleNamespace(language="en"))
    ctx.shared["whisper_model"] = model
    with patch("app.pipeline.steps.transcribe.extract_audio_wav"):
        TranscribeStep().run(ctx)
    assert model.transcribe.call_args.kwargs["word_timestamps"] is word_timestamps
    assert model.transcribe.call_args.kwargs["beam_size"] == beam_size
    result = ctx.shared["transcript"]
    assert result["word_timestamps"] is word_timestamps
    assert result["segments"][0]["avg_logprob"] == -.2
    if word_timestamps:
        assert result["segments"][0]["words"] == [{"start": 1.123, "end": 1.6, "word": "Hello", "probability": .97}]
    else:
        assert "words" not in result["segments"][0]


def test_transcription_cancellation_is_not_reclassified_as_failure(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    model = Mock()
    def cancelled_segments():
        raise JobCancelled("Stop requested")
        yield
    model.transcribe.return_value = (cancelled_segments(), SimpleNamespace(language="en"))
    ctx.shared["whisper_model"] = model
    with patch("app.pipeline.steps.transcribe.extract_audio_wav"):
        with pytest.raises(JobCancelled, match="Stop requested"):
            TranscribeStep().run(ctx)
    assert read_cached_transcript(ctx) is None


def test_caption_fetch_cancellation_is_not_swallowed_by_fallback(tmp_path, monkeypatch):
    from app.pipeline.steps.transcript import TranscriptSourceStep

    ctx = _ctx(tmp_path, monkeypatch)
    metadata = SimpleNamespace(raw={"id": "example", "subtitles": {"en": []}}, duration=20)
    with patch("app.pipeline.steps.transcript.download_captions", side_effect=JobCancelled("Stop captions")):
        with pytest.raises(JobCancelled, match="Stop captions"):
            TranscriptSourceStep()._try_captions(ctx, "https://www.youtube.com/watch?v=example", metadata, "en")


@pytest.mark.parametrize("options", [
    {"range_start_seconds": 10, "range_end_seconds": 10},
    {"range_start_seconds": 10, "range_end_seconds": 5},
    {"range_start_seconds": float("nan")},
    {"range_end_seconds": float("inf")},
    {"frame_bursts": [{"start_seconds": 10, "end_seconds": 5, "fps": 5}]},
    {"frame_bursts": [{"start_seconds": 0, "end_seconds": 1, "fps": 61}]},
    {"frame_bursts": [{"start_seconds": 0, "end_seconds": 1}] * 9},
    {"frame_budget": 20001}, {"interval_ms": 16}, {"analysis_objective": "x" * 2001},
])
def test_invalid_ranges_and_work_budgets_are_rejected(options):
    with pytest.raises(ValidationError):
        CreateJobOptions(**options)


def test_high_fps_and_bounded_bursts_are_valid_options():
    options = CreateJobOptions(interval_ms=17, frame_budget=20000, range_start_seconds=60,
                               range_end_seconds=120, frame_bursts=[{"start_seconds": 80, "end_seconds": 85, "fps": 60}])
    assert options.frame_bursts[0].fps == 60


def test_capabilities_uses_local_server_origin_helper(tmp_path, monkeypatch):
    from app.utils.runtime_capabilities import capabilities

    settings = get_settings()
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    response = Mock()
    response.json.return_value = {"models": [{"name": settings.OLLAMA_VISION_MODEL}]}
    with patch("app.utils.runtime_capabilities.httpx.get", return_value=response) as request:
        snapshot = capabilities()
    assert request.call_args.args[0] == "http://ollama:11434/api/tags"
    assert request.call_args.kwargs["trust_env"] is False
    assert snapshot["analysis"]["vision_available"] is True


def test_capabilities_does_not_probe_external_vision_host(tmp_path, monkeypatch):
    from app.utils.runtime_capabilities import capabilities

    monkeypatch.setattr(get_settings(), "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(get_settings(), "OLLAMA_BASE_URL", "https://external.example")
    with patch("app.utils.runtime_capabilities.httpx.get") as request:
        snapshot = capabilities()
    request.assert_not_called()
    assert snapshot["analysis"]["vision_available"] is False


def test_reports_are_optional_for_old_jobs_and_existing_transcript_still_downloads(db_session, storage):
    job = Job(original_filename="legacy.mp4", status="completed", current_step="completed", overall_progress=100)
    db_session.add(job)
    db_session.commit()
    storage.save_bytes(f"{job.id}/transcript/transcript.txt", b"Legacy transcript")
    client = TestClient(app)
    for suffix in ("analysis", "analysis/report?format=txt"):
        response = client.get(f"/api/jobs/{job.id}/{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert client.get(f"/api/jobs/{job.id}/transcript?format=txt").text == "Legacy transcript"


def test_analysis_report_endpoints_return_exact_artifacts(db_session, storage):
    job = Job(original_filename="current.mp4", status="completed", current_step="completed", overall_progress=100)
    db_session.add(job)
    db_session.commit()
    timeline = {"schema_version": "1.0", "status": "success", "items": []}
    storage.save_bytes(f"{job.id}/analysis/timeline.json", json.dumps(timeline).encode())
    storage.save_bytes(f"{job.id}/analysis/report.md", b"# Complete evidence\n")
    storage.save_bytes(f"{job.id}/analysis/report.txt", b"Complete evidence\n")
    client = TestClient(app)
    assert client.get(f"/api/jobs/{job.id}/analysis").json() == timeline
    for format, expected in (("md", b"# Complete evidence\n"), ("txt", b"Complete evidence\n"), ("json", json.dumps(timeline).encode())):
        response = client.get(f"/api/jobs/{job.id}/analysis/report?format={format}")
        assert response.status_code == 200
        assert response.content == expected
        assert "attachment" in response.headers["content-disposition"]
    assert client.get(f"/api/jobs/{job.id}/analysis/report?format=exe").status_code == 422


def test_reanalysis_creates_new_job_and_preserves_original_data(db_session, storage):
    import uuid
    from app.pipeline.steps.reuse_dataset import ReuseDatasetStep
    from app.pipeline.context import PipelineContext

    original = Job(id=str(uuid.uuid4()), original_filename="original.mp4", status="completed",
                   current_step="completed", options={"processing_profile": "fast", "ocr_enabled": True})
    db_session.add(original)
    db_session.commit()
    manifest = {"video": {"duration_seconds": 30, "has_audio": True}, "frame_count": 1,
                "frame_counts": {"adaptive": 1}, "extraction_params": {"frame_range": {"start_seconds": 5, "end_seconds": 25}}}
    frames = [{"frame": 0, "image": "frame.jpg", "timestamp": 12}]
    originals = {"manifest.json": json.dumps(manifest).encode(), "metadata/frames.json": json.dumps(frames).encode(),
                 "frames/frame.jpg": b"immutable image", "source/video.mp4": b"immutable video",
                 "transcript/transcript.json": json.dumps(_transcript()).encode(), "content/audio.json": b'{"value":"original"}'}
    for path, content in originals.items():
        storage.save_bytes(f"{original.id}/{path}", content)
    client = TestClient(app)
    with patch("app.api.routes.jobs._enqueue"):
        response = client.post(f"/api/jobs/{original.id}/reanalyze", json={"analysis_objective": "Read the menu", "processing_profile": "detailed"})
    assert response.status_code == 202
    new_id = response.json()["job_id"]
    assert new_id != original.id
    db_session.expire_all()
    new_job = db_session.get(Job, new_id)
    assert new_job.options["reuse_job_id"] == original.id
    assert db_session.get(Job, original.id).options == {"processing_profile": "fast", "ocr_enabled": True}
    ctx = PipelineContext(job_id=new_id, storage=storage, options=new_job.options,
                          log=lambda *args: None, set_step_progress=lambda *args: None,
                          should_cancel=lambda: False, update_job=lambda *args: None)
    ReuseDatasetStep().run(ctx)
    assert ctx.shared["frames"] == frames
    assert ctx.shared["transcript"]["segments"] == _transcript()["segments"]
    storage.save_bytes(f"{new_id}/metadata/frames.json", b"edited new metadata")
    storage.save_bytes(f"{new_id}/transcript/transcript.json", b"edited new transcript")
    storage.save_bytes(f"{new_id}/content/audio.json", b"edited new content")
    for path, content in originals.items():
        assert storage.get(f"{original.id}/{path}").read_bytes() == content


def test_manifest_and_timeline_expose_actual_sampling_reductions(tmp_path, monkeypatch):
    from app.pipeline.steps.analyze_visuals import AnalyzeVisualsStep
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx = _ctx(tmp_path, monkeypatch, ocr_enabled=False, range_end_seconds=10000)
    ctx.shared.update({"mode": "adaptive", "original_filename": "video.mp4", "stored_source_filename": "video.mp4",
                       "frames": [], "frame_count": 0, "transcript": _transcript(),
                       "frame_range": {"start_seconds": 0, "end_seconds": 20, "frame_budget": 500},
                       "visual_scan": {"sampled_frames": 20, "max_dimension": 320},
                       "adaptive_config": {"target_frames": 100, "selected_frames": 80},
                       "burst_config": {"budget_limited": True, "windows": []}})
    ctx.storage.save_bytes(ctx.shared["source_relative_path"], b"source")
    for filename in ("transcript.txt", "transcript.json", "subtitles.srt"):
        ctx.storage.save_bytes(ctx.job_relative("transcript", filename), b"transcript")
    AnalyzeVisualsStep().run(ctx)
    GenerateMetadataStep().run(ctx)
    timeline = json.loads(ctx.storage.get(ctx.job_relative("analysis", "timeline.json")).read_text())
    assert timeline["coverage"]["range_end_seconds"] == 20
    assert timeline["coverage"]["largest_visual_gap_seconds"] == 20
    for key in ("frame_range", "visual_scan", "adaptive_config", "burst_config"):
        assert ctx.shared["manifest"]["extraction_params"][key] == ctx.shared[key]
        assert timeline["provenance"]["extraction"][key] == ctx.shared[key]


def test_main_ai_dataset_download_includes_new_visual_evidence(db_session, storage):
    import io
    import zipfile

    job = Job(original_filename="current.mp4", status="completed", current_step="completed", overall_progress=100)
    db_session.add(job)
    db_session.commit()
    for path in ("frames/frame.jpg", "analysis/timeline.json", "analysis/report.md", "metadata/frames.json"):
        storage.save_bytes(f"{job.id}/{path}", b"evidence")
    response = TestClient(app).get(f"/api/jobs/{job.id}/download?asset=zip&visuals=frames")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
    assert {"analysis/timeline.json", "analysis/report.md", "metadata/frames.json"} <= names


def test_metadata_rebuild_preserves_effective_interval_and_reuse_provenance(tmp_path, monkeypatch):
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep

    ctx = _ctx(tmp_path, monkeypatch, interval_ms=200)
    ctx.shared.update({"mode": "interval", "original_filename": "video.mp4", "stored_source_filename": "video.mp4",
                       "frames": [], "frame_count": 0, "transcript": _transcript(),
                       "reused_from_job_id": "existing-original",
                       "interval_config": {"requested_interval_seconds": .2, "effective_interval_seconds": 3.0,
                                           "actual_average_fps": 1 / 3, "was_capped": True}})
    ctx.storage.save_bytes(ctx.shared["source_relative_path"], b"source")
    for filename in ("transcript.txt", "transcript.json", "subtitles.srt"):
        ctx.storage.save_bytes(ctx.job_relative("transcript", filename), b"transcript")
    GenerateMetadataStep().run(ctx)
    expected = ctx.shared["manifest"]["extraction_params"]["interval"]
    ctx.shared.pop("interval_config")
    ctx.shared.pop("reused_from_job_id")
    GenerateMetadataStep().run(ctx)
    assert ctx.shared["manifest"]["extraction_params"]["interval_ms"] == 3000
    assert ctx.shared["manifest"]["extraction_params"]["interval"] == expected
    assert ctx.shared["manifest"]["reused_from_job_id"] == "existing-original"
