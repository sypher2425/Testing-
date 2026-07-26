"""Round 1.6: the transcription path must never hang, must reuse the model
across sequential jobs, and must explain every abnormal exit."""
import time
import uuid
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.database import get_session
from app.models import PIPELINE_STEPS, Job
from app.pipeline.errors import PipelineFailedError
from app.utils.timeouts import (
    HeartbeatTicker,
    StepTimeout,
    describe_worker_exit,
    parse_worker_exit,
    time_limit,
)
from tests.test_pipeline_steps import make_ctx


class FakeSegment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeInfo:
    language = "en"


class FakeModel:
    """Stand-in for WhisperModel. Counts constructions so tests can prove the
    real model is loaded once and reused, not rebuilt per file."""

    construction_count = 0

    def __init__(self, *args, **kwargs):
        FakeModel.construction_count += 1

    def transcribe(self, *args, **kwargs):
        segments = [
            FakeSegment(0.0, 2.0, "first segment"),
            FakeSegment(2.0, 4.0, "second segment"),
            FakeSegment(4.0, 5.0, "third segment"),
        ]
        return iter(segments), FakeInfo()


@pytest.fixture(autouse=True)
def _clear_model_cache():
    from app.pipeline.steps import load_model

    load_model._model_cache.clear()
    FakeModel.construction_count = 0
    yield
    load_model._model_cache.clear()


# --------------------------------------------------------------- timeouts util


def test_time_limit_raises_step_timeout_on_overrun():
    with pytest.raises(StepTimeout) as exc_info:
        with time_limit(0.1, "took too long"):
            time.sleep(2)
    assert "took too long" in str(exc_info.value)


def test_time_limit_is_a_noop_when_disabled():
    with time_limit(0, "never fires"):
        pass  # must not raise
    with time_limit(None, "never fires"):
        pass


def test_time_limit_cleans_up_and_allows_reuse():
    with time_limit(5, "unused"):
        pass
    # A second guard still works — the first one's timer was cancelled.
    with pytest.raises(StepTimeout):
        with time_limit(0.1, "second guard"):
            time.sleep(1)


def test_heartbeat_ticker_calls_back_during_blocking_work():
    beats = []
    with HeartbeatTicker(0.05, lambda: beats.append(time.monotonic())):
        time.sleep(0.3)
    assert len(beats) >= 2, "ticker should fire repeatedly during a blocking call"


def test_heartbeat_ticker_survives_a_throwing_callback():
    def boom():
        raise RuntimeError("callback failed")

    with HeartbeatTicker(0.05, boom):
        time.sleep(0.2)  # must not propagate


def test_describe_worker_exit_identifies_oom_kill():
    message = describe_worker_exit(-9)
    assert message and "out-of-memory" in message
    assert "TRANSCRIPTION_CONCURRENCY" in message
    # 137 is the shell's 128+9 convention for the same thing.
    assert "out-of-memory" in describe_worker_exit(137)
    # An explicit signal number takes the same path.
    assert "out-of-memory" in describe_worker_exit(signal_number=9)
    assert describe_worker_exit(0) is None
    assert describe_worker_exit(None) is None


def test_parse_worker_exit_reads_named_fields_not_the_first_integer():
    """Regression: Celery says "signal 9 (SIGKILL)" — grabbing the first
    integer read that as an *exit code* of 9 and lost the OOM signal."""
    signal_number, exitcode = parse_worker_exit(
        "Worker exited prematurely: signal 9 (SIGKILL) Job: 0."
    )
    assert signal_number == 9
    assert exitcode is None

    signal_number, exitcode = parse_worker_exit("Worker exited prematurely: exitcode 155.")
    assert signal_number is None
    assert exitcode == 155

    assert parse_worker_exit("") == (None, None)


# ------------------------------------------------------- loading_model step


def test_loading_model_is_a_real_pipeline_state():
    assert "loading_model" in PIPELINE_STEPS
    assert PIPELINE_STEPS.index("loading_model") < PIPELINE_STEPS.index("transcribing")


def test_load_model_step_skips_when_no_audio(tmp_path):
    from app.pipeline.steps.load_model import LoadWhisperModelStep

    ctx, logs, state = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": False}

    with patch("faster_whisper.WhisperModel", FakeModel):
        LoadWhisperModelStep().run(ctx)

    assert FakeModel.construction_count == 0, "must not load a model for a silent video"
    assert state["step_progress"]["loading_model"] == 100


def test_load_model_step_loads_once_and_reuses(tmp_path):
    from app.pipeline.steps.load_model import LoadWhisperModelStep

    with patch("faster_whisper.WhisperModel", FakeModel):
        for _ in range(3):
            ctx, _, _ = make_ctx(tmp_path)
            ctx.shared["video"] = {"has_audio": True}
            LoadWhisperModelStep().run(ctx)

    assert FakeModel.construction_count == 1, "model must be reused across sequential jobs"


def test_load_model_timeout_produces_typed_failure(tmp_path):
    from app.pipeline.steps.load_model import LoadWhisperModelStep

    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True}

    def slow_model(*args, **kwargs):
        time.sleep(5)

    settings = get_settings()
    original = settings.WHISPER_MODEL_LOAD_TIMEOUT_SECONDS
    settings.WHISPER_MODEL_LOAD_TIMEOUT_SECONDS = 1
    try:
        with patch("faster_whisper.WhisperModel", slow_model):
            with pytest.raises(PipelineFailedError) as exc_info:
                LoadWhisperModelStep().run(ctx)
    finally:
        settings.WHISPER_MODEL_LOAD_TIMEOUT_SECONDS = original

    assert exc_info.value.code == "model_load_timeout"


def test_load_model_failure_produces_typed_failure(tmp_path):
    from app.pipeline.steps.load_model import LoadWhisperModelStep

    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True}

    def broken_model(*args, **kwargs):
        raise OSError("could not reach huggingface.co")

    with patch("faster_whisper.WhisperModel", broken_model):
        with pytest.raises(PipelineFailedError) as exc_info:
            LoadWhisperModelStep().run(ctx)

    assert exc_info.value.code == "model_load_failed"
    assert "huggingface" in exc_info.value.message


# ------------------------------------------------------------ transcribe step


def test_transcribe_reports_progress_per_segment(tmp_path):
    """The R1.6 fix for the frozen-at-20% symptom: progress must advance while
    segments stream in, and the heartbeat must fire."""
    from app.pipeline.steps import transcribe as transcribe_module

    ctx, _, state = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True, "duration_seconds": 5.0}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")
    ctx.shared["whisper_model"] = FakeModel()

    progress_history: list[int] = []
    original_setter = ctx.set_step_progress

    def recording_setter(step, pct):
        if step == "transcribing":
            progress_history.append(pct)
        original_setter(step, pct)

    object.__setattr__(ctx, "set_step_progress", recording_setter)

    with patch.object(transcribe_module, "extract_audio_wav"):
        transcribe_module.TranscribeStep().run(ctx)

    # 20 (after audio extraction) then a rising value per segment, then 90/100.
    assert 20 in progress_history
    mid = [p for p in progress_history if 20 < p < 90]
    assert len(mid) >= 3, f"expected per-segment progress updates, got {progress_history}"
    assert mid == sorted(mid), "progress must be monotonically increasing"
    assert ctx.shared["transcript"]["segments"][0]["text"] == "first segment"


def test_transcribe_timeout_produces_typed_failure(tmp_path):
    from app.pipeline.steps import transcribe as transcribe_module

    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True, "duration_seconds": 5.0}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    class HangingModel:
        def transcribe(self, *args, **kwargs):
            def slow_generator():
                time.sleep(5)
                yield FakeSegment(0, 1, "never reached")

            return slow_generator(), FakeInfo()

    ctx.shared["whisper_model"] = HangingModel()

    settings = get_settings()
    original = settings.WHISPER_TIMEOUT_SECONDS
    settings.WHISPER_TIMEOUT_SECONDS = 1
    try:
        with patch.object(transcribe_module, "extract_audio_wav"):
            with pytest.raises(PipelineFailedError) as exc_info:
                transcribe_module.TranscribeStep().run(ctx)
    finally:
        settings.WHISPER_TIMEOUT_SECONDS = original

    assert exc_info.value.code == "transcription_timeout"
    assert "WHISPER_TIMEOUT_SECONDS" in exc_info.value.message


# ------------------------------------------------- sequential jobs end-to-end


def _make_job(db, storage, sample_bytes=b"fake video") -> str:
    job_id = str(uuid.uuid4())
    dest = storage.get(f"{job_id}/source/video.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(sample_bytes)
    job = Job(
        id=job_id,
        original_filename="clip.mp4",
        stored_source_filename="video.mp4",
        status="queued",
        mode="interval",
        options={
            "mode": "interval",
            "interval_ms": 1000,
            "frame_format": "jpeg",
            "frame_max_dim": 320,
            "opening_dense_enabled": False,
        },
        step_progress={},
    )
    db.add(job)
    db.commit()
    return job_id


def test_two_jobs_run_sequentially_and_both_complete(db_session, storage):
    """The user's explicit requirement: two submitted jobs must both finish
    when the worker processes them one at a time, and the model must be
    constructed once — not reloaded for the second file."""
    from app.pipeline.runner import run_pipeline
    from app.utils.ffmpeg import ProbeResult

    job_ids = [_make_job(db_session, storage) for _ in range(2)]

    probe = ProbeResult(
        duration_seconds=5.0, width=320, height=240, fps=10.0, codec="h264", has_audio=True, raw={}
    )

    def fake_frame(source, output_path, timestamp, **kwargs):
        with open(output_path, "wb") as f:
            f.write(b"jpegbytes")

    with patch("app.pipeline.steps.probe.ffprobe", return_value=probe), patch(
        "faster_whisper.WhisperModel", FakeModel
    ), patch("app.pipeline.steps.transcribe.extract_audio_wav"), patch(
        "app.pipeline.steps.extract_frames.extract_frame_at", side_effect=fake_frame
    ):
        for job_id in job_ids:
            run_pipeline(job_id)

    session = get_session()
    try:
        for job_id in job_ids:
            job = session.get(Job, job_id)
            assert job.status == "completed", f"{job_id} ended as {job.status}: {job.error_message}"
            assert job.step_progress.get("loading_model") == 100
            assert job.step_progress.get("transcribing") == 100
    finally:
        session.close()

    assert FakeModel.construction_count == 1, "the second job must reuse the first job's model"


# ----------------------------------------------------------- startup recovery


def test_recover_interrupted_jobs_requeues_when_source_exists(db_session, storage):
    from app import tasks

    job_id = _make_job(db_session, storage)
    job = db_session.get(Job, job_id)
    job.status = "transcribing"  # stranded mid-flight by a restart
    job.started_at = job.created_at
    db_session.commit()

    with patch.object(tasks.process_job, "delay") as mock_delay:
        outcome = tasks.recover_interrupted_jobs()

    assert outcome["requeued"] == 1
    mock_delay.assert_called_once_with(job_id)
    db_session.expire_all()
    assert db_session.get(Job, job_id).status == "queued"


def test_recover_interrupted_jobs_fails_when_source_is_gone(db_session, storage):
    from app import tasks

    job_id = _make_job(db_session, storage)
    storage.delete(job_id)  # source video no longer on disk
    job = db_session.get(Job, job_id)
    job.status = "extracting_frames"
    job.started_at = job.created_at
    db_session.commit()

    with patch.object(tasks.process_job, "delay") as mock_delay:
        outcome = tasks.recover_interrupted_jobs()

    assert outcome["failed"] == 1
    mock_delay.assert_not_called()
    db_session.expire_all()
    recovered = db_session.get(Job, job_id)
    assert recovered.status == "failed"
    assert recovered.error_code == "interrupted"


def test_recovery_leaves_never_started_queued_jobs_alone(db_session, storage):
    """A job still sitting in the broker queue isn't stranded — don't touch it."""
    from app import tasks

    job_id = _make_job(db_session, storage)  # status=queued, started_at=None

    with patch.object(tasks.process_job, "delay") as mock_delay:
        outcome = tasks.recover_interrupted_jobs()

    assert outcome == {"requeued": 0, "failed": 0}
    mock_delay.assert_not_called()
    db_session.expire_all()
    assert db_session.get(Job, job_id).status == "queued"


# ------------------------------------------------------------ OOM diagnostics


def test_task_failure_handler_marks_oom_killed_job_failed(db_session, storage):
    from app import tasks

    job_id = _make_job(db_session, storage)
    job = db_session.get(Job, job_id)
    job.status = "transcribing"
    db_session.commit()

    class WorkerLostError(Exception):
        pass

    tasks._on_task_failure(
        task_id="task-1",
        exception=WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL) exitcode -9."),
        args=(job_id,),
    )

    db_session.expire_all()
    recovered = db_session.get(Job, job_id)
    assert recovered.status == "failed"
    assert recovered.error_code == "worker_out_of_memory"
    assert "out-of-memory" in recovered.error_message


def test_task_failure_handler_ignores_ordinary_exceptions(db_session, storage):
    from app import tasks

    job_id = _make_job(db_session, storage)
    job = db_session.get(Job, job_id)
    job.status = "transcribing"
    db_session.commit()

    tasks._on_task_failure(task_id="t", exception=ValueError("a normal bug"), args=(job_id,))

    db_session.expire_all()
    # The pipeline's own error handling owns this case; the signal handler
    # must not stomp on it.
    assert db_session.get(Job, job_id).status == "transcribing"


# ---------------------------------------------------------------- concurrency


def test_transcription_concurrency_defaults_to_one():
    assert get_settings().TRANSCRIPTION_CONCURRENCY == 1


def test_celery_worker_concurrency_uses_the_setting():
    from app.celery_app import celery_app

    assert celery_app.conf.worker_concurrency == get_settings().TRANSCRIPTION_CONCURRENCY
    assert celery_app.conf.task_time_limit == get_settings().TASK_HARD_TIME_LIMIT_SECONDS
