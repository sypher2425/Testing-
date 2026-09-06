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
import ctypes
import threading
import time

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.timeouts import HeartbeatTicker, StepTimeout, time_limit

# Process-local cache: one model instance reused by every job this worker
# child handles. Store the creating PID as well as the model because Celery's
# prefork pool copies Python globals into its children. Native CTranslate2
# models cannot safely be reused after that fork.
_model_cache: dict[str, tuple[int, object]] = {}
_model_cache_lock = threading.Lock()


def _cache_key(settings) -> str:
    return f"{settings.WHISPER_MODEL_SIZE}:{settings.WHISPER_DEVICE}:{settings.WHISPER_COMPUTE_TYPE}"


def execution_config(settings=None) -> tuple[str, str]:
    settings = settings or get_settings()
    device = settings.WHISPER_DEVICE
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
            if device == "cuda":
                ctypes.CDLL("cublas64_12.dll" if os.name == "nt" else "libcublas.so.12")
                ctypes.CDLL("cudnn64_9.dll" if os.name == "nt" else "libcudnn.so.9")
        except (ImportError, RuntimeError, OSError):
            device = "cpu"
    compute = settings.WHISPER_COMPUTE_TYPE
    if device == "cpu" and compute in {"float16", "int8_float16", "bfloat16", "int8_bfloat16"}:
        compute = "int8"
    return device, compute


def is_model_cached() -> bool:
    key = _cache_key(get_settings())
    with _model_cache_lock:
        cached = _model_cache.get(key)
        return cached is not None and cached[0] == os.getpid()


def clear_model_cache() -> None:
    """Drop references inherited from another process after Celery forks."""
    with _model_cache_lock:
        _model_cache.clear()


def load_whisper_model(log=None):
    """Returns the cached model, loading it (and downloading weights on first
    run) if needed. `log` is an optional (level, message) callable."""
    settings = get_settings()
    key = _cache_key(settings)

    # Warm-up and the first real job can arrive together. Serialize the load so
    # the worker never allocates two ~0.5-1GB models or observes a half-built
    # cache entry.
    with _model_cache_lock:
        cached = _model_cache.get(key)
        if cached is not None and cached[0] == os.getpid():
            if log:
                log("info", f"Whisper model '{settings.WHISPER_MODEL_SIZE}' reused from this worker's cache")
            return cached[1]

        # An entry with another PID was constructed before Celery forked. Do
        # not call into that inherited native object; replace it in this child.
        _model_cache.pop(key, None)

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

        device, compute = execution_config(settings)
        started = time.monotonic()
        model = WhisperModel(
            settings.WHISPER_MODEL_SIZE,
            device=device,
            compute_type=compute,
            download_root=settings.HF_HOME or None,
            cpu_threads=settings.WHISPER_CPU_THREADS,
        )
        _model_cache[key] = (os.getpid(), model)
        if log:
            log("info", f"Whisper model ready on {device}/{compute} in {time.monotonic() - started:.1f}s")
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

        from app.utils.transcript_cache import prepare_transcript
        if prepare_transcript(ctx):
            ctx.info("Transcript already available; model loading skipped.")
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
