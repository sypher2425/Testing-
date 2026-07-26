"""Loads (and on first run, downloads) the Whisper model as its own pipeline step.

Why this is a separate step rather than part of TranscribeStep:
- The download is ~460MB for `small`. Folding it into "Transcribing audio"
  made a multi-minute download indistinguishable from a hang.
- As a real PipelineStep it gets its own job status (`loading_model`), its
  own progress bar, and its own heartbeat — so the UI shows what's actually
  happening and the stale-job reaper can tell "working" from "dead".

The model is cached per worker process and reused across sequential jobs;
the log line says explicitly whether it was loaded or reused.
"""
import os
import time

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.timeouts import HeartbeatTicker, StepTimeout, time_limit

# Process-local cache: one model instance reused by every job this worker
# child handles. Keyed by the settings that affect the weights themselves.
_model_cache: dict[str, object] = {}


def _cache_key(settings) -> str:
    return f"{settings.WHISPER_MODEL_SIZE}:{settings.WHISPER_DEVICE}:{settings.WHISPER_COMPUTE_TYPE}"


def is_model_cached() -> bool:
    return _cache_key(get_settings()) in _model_cache


def load_whisper_model(log=None):
    """Returns the cached model, loading it (and downloading weights on first
    run) if needed. `log` is an optional (level, message) callable."""
    settings = get_settings()
    key = _cache_key(settings)

    if key in _model_cache:
        if log:
            log("info", f"Whisper model '{settings.WHISPER_MODEL_SIZE}' reused from this worker's cache")
        return _model_cache[key]

    # huggingface_hub reads these at import/download time; setting them here
    # means one place controls the cache location and socket timeout.
    os.environ.setdefault("HF_HOME", settings.HF_HOME)
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(settings.HF_HUB_DOWNLOAD_TIMEOUT))

    from faster_whisper import WhisperModel

    if log:
        log(
            "info",
            f"Loading Whisper model '{settings.WHISPER_MODEL_SIZE}' "
            f"(device={settings.WHISPER_DEVICE}, compute_type={settings.WHISPER_COMPUTE_TYPE}). "
            f"First run downloads the weights to {settings.HF_HOME} — this can take several "
            "minutes on a slow connection; later runs reuse the cache.",
        )

    started = time.monotonic()
    model = WhisperModel(
        settings.WHISPER_MODEL_SIZE,
        device=settings.WHISPER_DEVICE,
        compute_type=settings.WHISPER_COMPUTE_TYPE,
        download_root=settings.HF_HOME or None,
    )
    _model_cache[key] = model
    if log:
        log("info", f"Whisper model ready in {time.monotonic() - started:.1f}s")
    return model


class LoadWhisperModelStep(PipelineStep):
    name = "loading_model"
    label = "Loading transcription model"
    consumes = ("video",)
    produces = ("whisper_model",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        video_meta = ctx.shared.get("video") or {}

        if not video_meta.get("has_audio"):
            ctx.info("Video has no audio track; skipping model load.")
            ctx.set_step_progress(self.name, 100)
            return

        settings = get_settings()
        try:
            # Heartbeat keeps last_heartbeat fresh through the blocking load
            # so the reaper doesn't mistake a long download for a dead worker.
            with HeartbeatTicker(settings.HEARTBEAT_INTERVAL_SECONDS, ctx.heartbeat):
                with time_limit(
                    settings.WHISPER_MODEL_LOAD_TIMEOUT_SECONDS,
                    f"Loading the Whisper model exceeded "
                    f"WHISPER_MODEL_LOAD_TIMEOUT_SECONDS ({settings.WHISPER_MODEL_LOAD_TIMEOUT_SECONDS}s). "
                    "The model download may be stalled — check the worker's network access.",
                ):
                    ctx.shared["whisper_model"] = load_whisper_model(log=ctx.log)
        except StepTimeout as exc:
            raise PipelineFailedError("model_load_timeout", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface a typed failure, never hang
            raise PipelineFailedError(
                "model_load_failed",
                f"Could not load the Whisper model '{settings.WHISPER_MODEL_SIZE}': {exc}",
                {"error": str(exc)},
            ) from exc

        ctx.set_step_progress(self.name, 100)
