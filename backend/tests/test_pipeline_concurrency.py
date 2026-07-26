"""The runner's DB callbacks are called from two threads at once.

HeartbeatTicker runs `ctx.heartbeat` on a daemon thread while the step's own
thread writes progress (per transcript segment, per zipped file). A single
SQLAlchemy Session shared between them corrupts its transaction state and
every later statement dies with "This session is in 'prepared' state" — which
the step then reports as "faster-whisper failed", pointing at the wrong
component entirely. These tests pin the concurrency contract.
"""
import threading
import time
from unittest.mock import patch

import pytest

from app.database import get_session
from app.models import Job
from app.pipeline.runner import run_pipeline
from app.utils.timeouts import HeartbeatTicker


def _make_job(**kwargs) -> str:
    session = get_session()
    try:
        job = Job(
            original_filename="concurrent.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            status="queued",
            current_step="queued",
            step_progress={},
            **kwargs,
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


class _CallbackCapture:
    """Runs a real run_pipeline far enough to hand us the live callbacks, then
    aborts the pipeline — we want the closures, not the work."""

    def __init__(self):
        self.ctx = None

    def capture_step(self):
        capture = self

        class CaptureStep:
            name = "probing"
            label = "Probing video"
            consumes = ()
            produces = ()

            def run(self, ctx):
                capture.ctx = ctx
                raise _StopPipeline()

        return CaptureStep()


class _StopPipeline(Exception):
    pass


def _live_ctx(job_id):
    """Get a PipelineContext wired to the real runner callbacks for job_id."""
    capture = _CallbackCapture()
    with patch("app.pipeline.runner._build_pipeline", return_value=[capture.capture_step()]):
        run_pipeline(job_id)
    assert capture.ctx is not None
    return capture.ctx


def test_heartbeat_thread_and_progress_writes_do_not_corrupt_the_session():
    """Regression: this reproduces the reported production failure. On the
    pre-fix runner (one Session shared across threads) it raises
    "This session is in 'prepared' state; no further SQL can be emitted"."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    errors: list[BaseException] = []
    beats = {"count": 0}

    def counting_heartbeat():
        ctx.heartbeat()
        beats["count"] += 1

    # Ticker hammering from a second thread, exactly like transcribe/zip do.
    with HeartbeatTicker(0.01, counting_heartbeat):
        deadline = time.time() + 2.0
        pct = 0
        while time.time() < deadline:
            try:
                pct = (pct + 1) % 101
                ctx.set_step_progress("transcribing", pct)
                ctx.check_cancel()  # exercises should_cancel too
            except BaseException as exc:  # noqa: BLE001 - record and stop
                errors.append(exc)
                break

    assert not errors, f"concurrent DB access raised: {errors[0]!r}"
    assert beats["count"] > 0, "the ticker never fired; the test proves nothing"

    session = get_session()
    try:
        job = session.get(Job, job_id)
        assert "transcribing" in (job.step_progress or {})
        assert job.last_heartbeat is not None
    finally:
        session.close()


def test_log_writes_are_thread_safe_alongside_the_ticker():
    """persist_log inserts rows on the main thread while the ticker updates the
    job row — the other half of the same collision."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    errors: list[BaseException] = []
    with HeartbeatTicker(0.01, ctx.heartbeat):
        for i in range(150):
            try:
                ctx.info(f"log line {i}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                break

    assert not errors, f"concurrent log write raised: {errors[0]!r}"


def test_zip_step_progress_survives_its_own_heartbeat_ticker(tmp_path):
    """zip_output writes per-file progress inside a HeartbeatTicker — the same
    pattern that broke transcription, on a step nobody has stressed yet."""
    from app.pipeline.steps.zip_output import ZipOutputStep

    job_id = _make_job()
    ctx = _live_ctx(job_id)

    # Enough members that the progress loop runs many times while ticking.
    ctx.storage.save_bytes(ctx.job_relative("manifest.json"), b"{}")
    for i in range(120):
        ctx.storage.save_bytes(ctx.job_relative("frames", f"{i:04d}.jpg"), b"jpgbytes")

    with patch("app.pipeline.steps.zip_output.get_settings") as fake_settings:
        settings = fake_settings.return_value
        settings.ZIP_INCLUDE_SOURCE_VIDEO = False
        settings.HEARTBEAT_INTERVAL_SECONDS = 0.01  # tick constantly
        ZipOutputStep().run(ctx)

    session = get_session()
    try:
        assert session.get(Job, job_id).step_progress.get("zipping") == 100
    finally:
        session.close()


def test_progress_failures_never_kill_the_step():
    """A bookkeeping write is cosmetic; losing hours of transcription because
    one failed is not acceptable."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    with patch("app.pipeline.runner.get_session", side_effect=RuntimeError("db gone")):
        ctx.set_step_progress("transcribing", 42)  # must not raise
        ctx.heartbeat()  # must not raise


def test_update_job_still_propagates_failures():
    """Unlike progress, update_job carries real results — it must not fail silently."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    with patch("app.pipeline.runner.get_session", side_effect=RuntimeError("db gone")):
        with pytest.raises(RuntimeError):
            ctx.update_job({"language": "en"})


def test_cancellation_is_still_observed_across_a_fresh_session():
    """should_cancel must see a flag set by another connection (the API), which
    is exactly what the old expire_all() call was working around."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    assert ctx.should_cancel() is False

    other = get_session()
    try:
        other.get(Job, job_id).cancel_requested = True
        other.commit()
    finally:
        other.close()

    assert ctx.should_cancel() is True


def test_many_threads_do_not_deadlock_or_corrupt():
    """Belt-and-braces: several tickers at once, as could happen if a future
    step nests them."""
    job_id = _make_job()
    ctx = _live_ctx(job_id)

    errors: list[BaseException] = []
    stop = threading.Event()

    def worker(step_name):
        pct = 0
        while not stop.is_set():
            try:
                pct = (pct + 1) % 101
                ctx.set_step_progress(step_name, pct)
                ctx.heartbeat()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [
        threading.Thread(target=worker, args=(name,), daemon=True)
        for name in ("transcribing", "extracting_frames", "zipping")
    ]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"concurrent access raised: {errors[0]!r}"


def test_transcription_failure_message_names_the_exception_type(tmp_path):
    """The production report said "faster-whisper failed: This session is in
    'prepared' state" — a database error wearing Whisper's name. The type must
    be visible so the next one points at the right component."""
    from app.pipeline.errors import PipelineFailedError
    from app.pipeline.steps import transcribe as transcribe_module
    from tests.test_pipeline_steps import make_ctx

    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True, "duration_seconds": 2.5}
    ctx.storage.save_bytes(ctx.job_relative("source", "video.mp4"), b"fake")
    ctx.shared["source_relative_path"] = ctx.job_relative("source", "video.mp4")

    class ExplodingModel:
        def transcribe(self, *a, **k):
            from sqlalchemy.exc import IllegalStateChangeError

            raise IllegalStateChangeError("This session is in 'prepared' state")

    with patch("app.pipeline.steps.transcribe.extract_audio_wav"), patch.object(
        transcribe_module, "_get_whisper_model", return_value=ExplodingModel()
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            transcribe_module.TranscribeStep().run(ctx)

    assert exc_info.value.code == "transcription_failed"
    assert "IllegalStateChangeError" in exc_info.value.message
    assert exc_info.value.detail["error_type"] == "IllegalStateChangeError"
