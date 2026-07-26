"""Transcription step: local faster-whisper, optional WhisperX diarization.

The model itself is loaded by LoadWhisperModelStep (which runs immediately
before this step) so the download and the transcription are separately
visible, separately timed out, and separately reported in the UI.
"""
import json
import tempfile
from pathlib import Path

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.ffmpeg import FFmpegError, extract_audio_wav
from app.utils.timeouts import HeartbeatTicker, StepTimeout, time_limit

# Progress band this step reports within: audio extraction takes it to
# TRANSCRIBE_PROGRESS_START, then per-segment progress fills the rest.
TRANSCRIBE_PROGRESS_START = 20
TRANSCRIBE_PROGRESS_END = 70


def _get_whisper_model(log=None):
    """Delegates to the shared loader so the model is loaded once per worker
    process and reused across sequential jobs."""
    from app.pipeline.steps.load_model import load_whisper_model

    return load_whisper_model(log=log)


def _format_srt_timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{_format_srt_timestamp(seg['start'])} --> {_format_srt_timestamp(seg['end'])}")
        text = seg["text"]
        if seg.get("speaker"):
            text = f"[{seg['speaker']}] {text}"
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _maybe_diarize(segments: list[dict], audio_path: str, ctx: PipelineContext) -> list[dict]:
    settings = get_settings()
    if not settings.ENABLE_DIARIZATION:
        return segments
    try:
        import whisperx  # noqa: F401
    except ImportError:
        ctx.warning(
            "ENABLE_DIARIZATION is set but whisperx is not installed; "
            "skipping diarization. See README for optional setup."
        )
        return segments

    try:
        import torch
        import whisperx

        device = "cuda" if settings.WHISPER_DEVICE == "cuda" else "cpu"
        diarize_model = whisperx.DiarizationPipeline(
            use_auth_token=settings.HF_TOKEN or None, device=device
        )
        diarization = diarize_model(audio_path)
        result = {"segments": segments}
        aligned = whisperx.assign_word_speakers(diarization, result)
        return aligned.get("segments", segments)
    except Exception as exc:  # noqa: BLE001
        ctx.warning(f"Diarization failed, continuing without speaker labels: {exc}")
        return segments


class TranscribeStep(PipelineStep):
    name = "transcribing"
    label = "Transcribing audio"
    consumes = ("video",)
    produces = ("transcript",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        video_meta = ctx.shared["video"]

        if not video_meta.get("has_audio"):
            ctx.info("Video has no audio track; skipping transcription.")
            transcript = {
                "language": None,
                "duration": video_meta.get("duration_seconds"),
                "skipped": True,
                "skipped_reason": "no_audio_track",
                "segments": [],
            }
            self._write_outputs(ctx, transcript)
            ctx.shared["transcript"] = transcript
            ctx.shared["language"] = None
            ctx.set_step_progress(self.name, 100)
            return

        source_path = str(ctx.storage.get(ctx.shared["source_relative_path"]))
        settings = get_settings()

        with tempfile.TemporaryDirectory() as tmp:
            audio_path = str(Path(tmp) / "audio.wav")
            ctx.set_step_progress(self.name, 5)
            try:
                extract_audio_wav(source_path, audio_path, timeout=settings.FFMPEG_TIMEOUT_SECONDS)
            except FFmpegError as exc:
                raise PipelineFailedError(
                    "audio_extraction_failed", exc.message, exc.to_detail()
                ) from exc

            ctx.set_step_progress(self.name, TRANSCRIBE_PROGRESS_START)
            duration = video_meta.get("duration_seconds") or 0.0
            try:
                # The model is normally already warm from LoadWhisperModelStep;
                # this is a cache hit unless that step was skipped.
                model = ctx.shared.get("whisper_model") or _get_whisper_model(log=ctx.log)

                # Two guards around the same blocking work:
                #  - HeartbeatTicker proves liveness to the stale-job reaper
                #  - time_limit bounds the whole loop so it can never hang forever
                with HeartbeatTicker(settings.HEARTBEAT_INTERVAL_SECONDS, ctx.heartbeat):
                    with time_limit(
                        settings.WHISPER_TIMEOUT_SECONDS,
                        f"Transcription exceeded WHISPER_TIMEOUT_SECONDS "
                        f"({settings.WHISPER_TIMEOUT_SECONDS}s) and was aborted.",
                    ):
                        segments_iter, info = model.transcribe(
                            audio_path, word_timestamps=True, vad_filter=True
                        )
                        segments: list[dict] = []
                        # faster-whisper yields lazily: the real work happens as
                        # we iterate, so this is where progress actually moves.
                        for seg in segments_iter:
                            ctx.check_cancel()
                            segments.append(
                                {
                                    "start": round(seg.start, 3),
                                    "end": round(seg.end, 3),
                                    "text": seg.text.strip(),
                                }
                            )
                            if duration > 0:
                                fraction = min(max(seg.end / duration, 0.0), 1.0)
                                ctx.set_step_progress(
                                    self.name,
                                    TRANSCRIBE_PROGRESS_START
                                    + round((TRANSCRIBE_PROGRESS_END - TRANSCRIBE_PROGRESS_START) * fraction),
                                )
                            else:
                                ctx.heartbeat()
                        language = info.language
            except StepTimeout as exc:
                raise PipelineFailedError("transcription_timeout", str(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                raise PipelineFailedError(
                    "transcription_failed", f"faster-whisper failed: {exc}", {"error": str(exc)}
                ) from exc

            ctx.set_step_progress(self.name, 70)
            segments = _maybe_diarize(segments, audio_path, ctx)

        ctx.set_step_progress(self.name, 90)
        transcript = {
            "language": language,
            "duration": video_meta.get("duration_seconds"),
            "skipped": False,
            "skipped_reason": None,
            "segments": segments,
        }
        self._write_outputs(ctx, transcript)
        ctx.shared["transcript"] = transcript
        ctx.shared["language"] = language
        ctx.update_job({"language": language})
        ctx.info(f"Transcription complete: language={language}, {len(segments)} segments")
        ctx.set_step_progress(self.name, 100)

    def _write_outputs(self, ctx: PipelineContext, transcript: dict) -> None:
        segments = transcript["segments"]
        txt = "\n".join(s["text"] for s in segments) if segments else ""
        srt = _build_srt(segments) if segments else ""

        ctx.storage.save_bytes(
            ctx.job_relative("transcript", "transcript.json"),
            json.dumps(transcript, indent=2).encode(),
        )
        ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.txt"), txt.encode())
        ctx.storage.save_bytes(ctx.job_relative("transcript", "subtitles.srt"), srt.encode())
