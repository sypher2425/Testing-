"""Content-addressed transcript reuse, independent of frame/export settings."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from app.config import get_settings


def cache_key(ctx) -> str | None:
    settings = get_settings()
    if not settings.TRANSCRIPT_CACHE_ENABLED or settings.ENABLE_DIARIZATION:
        return None
    source_hash = ctx.shared.get("source_sha256")
    if not source_hash:
        return None
    params = ctx.options.get("transcript") or {}
    config = {
        "version": 2, "source": source_hash,
        "engine": "faster-whisper-1.2.1",
        "model": settings.WHISPER_MODEL_SIZE,
        "compute_type": settings.WHISPER_COMPUTE_TYPE,
        "profile": ctx.options.get("processing_profile", "balanced"),
        "preference": params.get("source_preference", ctx.options.get("source_preference", "captions_first")),
        "language": params.get("language"),
        "batch_size": settings.WHISPER_BATCH_SIZE,
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def read_cached_transcript(ctx):
    key = cache_key(ctx)
    if key is None:
        return None
    path = get_settings().data_path / "transcript_cache" / f"{key}.json"
    try:
        if time.time() - path.stat().st_mtime > get_settings().ANALYSIS_CACHE_RETENTION_HOURS * 3600:
            return None
        transcript = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(transcript, dict) or not isinstance(transcript.get("segments"), list):
            return None
        transcript["cache_hit"] = True
        return transcript
    except (OSError, ValueError):
        return None


def write_cached_transcript(ctx, transcript):
    key = cache_key(ctx)
    if key is None or transcript.get("skipped"):
        return
    directory = get_settings().data_path / "transcript_cache"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({**transcript, "cache_hit": False}, handle, ensure_ascii=False)
        os.replace(temporary, directory / f"{key}.json")
    except OSError as exc:
        ctx.warning(f"Transcript cache could not be saved: {exc}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_transcript(ctx) -> bool:
    """Resolve cached/caption results before paying for a model load."""
    if ctx.shared.get("prepared_transcript") or ctx.shared.get("caption_transcript"):
        return True
    cached = read_cached_transcript(ctx)
    if cached is not None:
        ctx.shared["prepared_transcript"] = cached
        ctx.info("Reusing transcript from identical source and transcription settings.")
        return True
    if ctx.shared.get("caption_lookup_done"):
        return False
    ctx.shared["caption_lookup_done"] = True
    metadata = ctx.shared.get("caption_metadata")
    url = ctx.shared.get("source_url")
    if url and metadata is not None and ctx.options.get("source_preference", "captions_first") == "captions_first":
        from app.pipeline.steps.transcript import TranscriptSourceStep
        if TranscriptSourceStep()._try_captions(ctx, url, metadata, None):
            result = ctx.shared["caption_transcript"]
            result["source"] = "platform_captions"
            result["word_timestamps"] = False
            ctx.shared["prepared_transcript"] = result
            return True
    return False
