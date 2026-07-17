"""Transcription step: local faster-whisper, optional WhisperX diarization."""
import json
import tempfile
from pathlib import Path

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.ffmpeg import FFmpegError, extract_audio_wav

_model_cache: dict[str, object] = {}


def _get_whisper_model():
    settings = get_settings()
    key = f"{settings.WHISPER_MODEL_SIZE}:{settings.WHISPER_DEVICE}:{settings.WHISPER_COMPUTE_TYPE}"
    if key not in _model_cache:
        from faster_whisper import WhisperModel

        _model_cache[key] = WhisperModel(
            settings.WHISPER_MODEL_SIZE,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
        )
    return _model_cache[key]


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

            ctx.set_step_progress(self.name, 20)
            try:
                model = _get_whisper_model()
                segments_iter, info = model.transcribe(
                    audio_path, word_timestamps=True, vad_filter=True
                )
                segments: list[dict] = []
                for seg in segments_iter:
                    ctx.check_cancel()
                    segments.append(
                        {
                            "start": round(seg.start, 3),
                            "end": round(seg.end, 3),
                            "text": seg.text.strip(),
                        }
                    )
                language = info.language
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
