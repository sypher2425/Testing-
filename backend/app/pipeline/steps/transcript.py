"""Transcript mode: paste any platform link, get a transcript.

The cheap path first. Most platforms already carry a caption track, and
downloading a VTT file costs a fraction of a second and no audio bytes at
all — so this pipeline asks for captions before it asks for media, and only
falls back to downloading audio and running Whisper when there are none.

Two deliberate constraints:

- **Audio only, never the video.** Whisper never sees pixels, so transcript
  mode downloads `-f ba/bestaudio` instead of the full stream. On a long
  video that is the difference between a few MB and a few GB.
- **One transcript shape, whatever the source.** Platform captions are
  parsed into the same {start, end, text} segments Whisper produces, so
  `transcript/transcript.json`, `transcript.txt` and `subtitles.srt` are
  written by the same code either way, and `/api/jobs/{id}/transcript`
  needs no special case. Which source was used is recorded explicitly in
  `transcript_source` rather than being left for the reader to infer.

Step names are reused from the video pipeline (`fetching_source`,
`loading_model`, `transcribing`, `generating_metadata`, `zipping`) so no new
job states, status chips or progress labels are needed anywhere.
"""
import json
import tempfile
from pathlib import Path

from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.pipeline.steps.load_model import LoadWhisperModelStep
from app.pipeline.steps.transcribe import TranscribeStep
from app.utils.captions import subtitles_to_segments, subtitles_to_text
from app.utils.timestamps import now_utc_iso
from app.utils.url_canonical import canonicalize
from app.utils.ytdlp import (
    YtDlpError,
    cookies_status,
    download_audio,
    download_captions,
    extract_metadata,
)

#: How the transcript was produced. Recorded verbatim in transcript.json.
SOURCE_PLATFORM_CAPTIONS = "platform_captions"
SOURCE_WHISPER = "whisper"

#: What the requester asked for. `captions_first` is the default: try the
#: platform's own captions, fall back to Whisper. The other two are explicit
#: opt-outs — `captions_only` never downloads audio, `whisper_only` ignores
#: any captions the platform offers (useful when they're known to be bad).
PREFERENCES = ("captions_first", "captions_only", "whisper_only")

def _caption_langs(requested: str | None) -> str:
    """yt-dlp --sub-langs selector. A requested language wins; otherwise
    prefer English but accept whatever single track exists, so a non-English
    video still yields a transcript."""
    if requested:
        return f"{requested}.*,{requested}"
    return "en.*,en"


class TranscriptSourceStep(PipelineStep):
    """Resolve the link: metadata, then captions or audio."""

    name = "fetching_source"
    label = "Fetching source"
    produces = ("transcript_meta", "video")

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        params = ctx.options.get("transcript") or {}
        url = params.get("url")
        if not url:
            raise PipelineFailedError("invalid_request", "Transcript mode requires a source URL")

        preference = params.get("source_preference") or "captions_first"
        language = params.get("language")

        ctx.info(f"yt-dlp cookies: {cookies_status()}")
        try:
            metadata = extract_metadata(url, log=ctx.log)
        except YtDlpError as exc:
            ctx.error(f"yt-dlp metadata fetch failed [{exc.code}]: {exc.message}")
            if exc.stderr:
                ctx.error(f"yt-dlp stderr (last 1500 chars): {exc.stderr.strip()[-1500:]}")
            raise PipelineFailedError(exc.code, exc.message, exc.to_detail()) from exc

        canonical = canonicalize(url)
        ctx.shared["url_canonical"] = {
            "original": canonical.original,
            "canonical": canonical.canonical,
            "platform": canonical.platform,
            "platform_post_id": canonical.post_id,
        }
        ctx.shared["transcript_meta"] = {
            "source_url": url,
            "platform": metadata.platform,
            "title": metadata.title,
            "uploader": metadata.uploader,
            "upload_date": metadata.upload_date,
            "duration_seconds": metadata.duration,
            "fetched_at": now_utc_iso(),
        }
        ctx.info(
            f"Resolved {metadata.platform} link: {metadata.title!r} "
            f"({metadata.duration or '?'}s)"
        )

        update_fields = {"source_url": url, "duration_seconds": metadata.duration}
        if metadata.title:
            update_fields["original_filename"] = metadata.title[:512]
        ctx.update_job(update_fields)
        ctx.set_step_progress(self.name, 25)

        # `video` is what LoadWhisperModelStep and TranscribeStep read. There
        # is no probe in this pipeline, so it is populated from the link's own
        # metadata; has_audio is True because a video with no audio at all
        # cannot be transcribed and yt-dlp would have nothing to hand over.
        ctx.shared["video"] = {
            "duration_seconds": metadata.duration,
            "has_audio": True,
        }

        if preference != "whisper_only":
            ctx.check_cancel()
            if self._try_captions(ctx, url, metadata, language):
                ctx.set_step_progress(self.name, 100)
                return
            if preference == "captions_only":
                raise PipelineFailedError(
                    "captions_unavailable",
                    "No caption track was available for this link, and "
                    "'captions_only' was requested so audio was not downloaded. "
                    "Re-run with the default preference to transcribe it with Whisper.",
                )

        ctx.check_cancel()
        ctx.info("Downloading audio only (no video stream) for transcription…")
        source_dir = ctx.storage.get(ctx.job_relative("source"))
        try:
            audio_path = download_audio(url, source_dir, log=ctx.log)
        except YtDlpError as exc:
            ctx.error(f"yt-dlp audio download failed [{exc.code}]: {exc.message}")
            raise PipelineFailedError(exc.code, exc.message, exc.to_detail()) from exc

        ctx.shared["stored_source_filename"] = audio_path.name
        ctx.shared["source_relative_path"] = ctx.job_relative("source", audio_path.name)
        ctx.update_job(
            {
                "stored_source_filename": audio_path.name,
                "file_size_bytes": audio_path.stat().st_size,
            }
        )
        ctx.info(
            f"Audio downloaded: {audio_path.name} "
            f"({audio_path.stat().st_size / (1024 * 1024):.1f}MB)"
        )
        ctx.set_step_progress(self.name, 100)

    def _try_captions(self, ctx: PipelineContext, url: str, metadata, language: str | None) -> bool:
        """Returns True when a usable caption track produced a transcript.

        Never raises: every failure here is a reason to fall back to Whisper,
        not a reason to fail the job."""
        video_id = str(metadata.raw.get("id") or "").strip()
        sub_langs = _caption_langs(language)
        available = {
            "manual": bool(metadata.raw.get("subtitles")),
            "automatic": bool(metadata.raw.get("automatic_captions")),
        }
        if not available["manual"] and not available["automatic"]:
            ctx.info("The platform advertises no caption track; transcribing the audio instead.")
            return False

        for track in ("manual", "automatic"):
            if not available[track]:
                continue
            ctx.check_cancel()
            ctx.info(f"Trying {track} captions ({sub_langs})…")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    sub_path = download_captions(
                        url,
                        Path(tmp),
                        video_id,
                        manual=(track == "manual"),
                        log=ctx.log,
                        sub_langs=sub_langs,
                    )
                    if sub_path is None:
                        ctx.info(f"No {track} caption file was produced for {sub_langs}.")
                        continue
                    raw = sub_path.read_text(encoding="utf-8", errors="replace")
                    caption_language = self._language_from_filename(sub_path.name, video_id)
            except YtDlpError as exc:
                ctx.warning(f"{track.capitalize()} caption fetch failed: {exc.message}")
                continue
            except Exception as exc:  # noqa: BLE001 - always fall back, never fail here
                ctx.warning(f"{track.capitalize()} caption fetch failed: {exc}")
                continue

            segments = subtitles_to_segments(raw)
            if not segments:
                ctx.warning(f"{track.capitalize()} captions were empty after cleaning.")
                continue

            ctx.shared["caption_transcript"] = {
                "language": caption_language or language,
                "duration": metadata.duration,
                "skipped": False,
                "skipped_reason": None,
                "transcript_source": SOURCE_PLATFORM_CAPTIONS,
                "caption_track": track,
                "segments": segments,
            }
            ctx.shared["caption_plain_text"] = subtitles_to_text(raw)
            ctx.info(
                f"Using the platform's {track} captions: {len(segments)} segments, "
                "no audio download needed."
            )
            return True

        return False

    @staticmethod
    def _language_from_filename(filename: str, video_id: str) -> str | None:
        """yt-dlp names caption files '<id>.<lang>.<ext>' — the only place the
        actual delivered language is reported."""
        stem = filename.rsplit(".", 1)[0]
        if video_id and stem.startswith(f"{video_id}."):
            stem = stem[len(video_id) + 1 :]
        return stem.split(".")[-1] or None


class TranscriptModelStep(LoadWhisperModelStep):
    """Loading a ~460MB model to transcribe nothing is pure waste, so this
    is skipped outright when captions already produced the transcript."""

    def run(self, ctx: PipelineContext) -> None:
        if ctx.shared.get("caption_transcript"):
            ctx.set_step_progress(self.name, 100)
            return
        super().run(ctx)


class TranscriptTranscribeStep(TranscribeStep):
    """Whisper, unless the captions path already produced a transcript — in
    which case this step only writes it out in the three standard formats."""

    def run(self, ctx: PipelineContext) -> None:
        caption_transcript = ctx.shared.get("caption_transcript")
        if not caption_transcript:
            super().run(ctx)
            # Record provenance on the JSON the parent already wrote. Only
            # transcript.json carries these fields, so the .txt/.srt files it
            # produced are still correct and are not rewritten.
            transcript = ctx.shared.get("transcript") or {}
            transcript["transcript_source"] = SOURCE_WHISPER
            transcript["caption_track"] = None
            ctx.storage.save_bytes(
                ctx.job_relative("transcript", "transcript.json"),
                json.dumps(transcript, indent=2).encode(),
            )
            ctx.shared["transcript"] = transcript
            return

        ctx.set_step_progress(self.name, 10)
        self._write_outputs(ctx, caption_transcript)
        # The plain-text file gets the paragraph-merged version rather than
        # one caption cue per line — the cues are a display artifact, and
        # nobody wants to read (or feed a model) 900 three-word lines.
        plain = ctx.shared.get("caption_plain_text")
        if plain:
            ctx.storage.save_bytes(ctx.job_relative("transcript", "transcript.txt"), plain.encode())
        ctx.shared["transcript"] = caption_transcript
        ctx.shared["language"] = caption_transcript.get("language")
        ctx.update_job({"language": caption_transcript.get("language")})
        ctx.info(
            f"Transcript written from platform captions: "
            f"{len(caption_transcript['segments'])} segments"
        )
        ctx.set_step_progress(self.name, 100)


class TranscriptManifestStep(PipelineStep):
    name = "generating_metadata"
    label = "Generating manifest"
    consumes = ("transcript",)
    produces = ("manifest",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 10)
        transcript = ctx.shared.get("transcript") or {}
        meta = ctx.shared.get("transcript_meta") or {}
        segments = transcript.get("segments") or []
        params = ctx.options.get("transcript") or {}

        manifest = {
            "schema_version": "transcript-1.0",
            "job_type": "transcript",
            "source": {
                **meta,
                "url_canonical": ctx.shared.get("url_canonical"),
                "requested_preference": params.get("source_preference") or "captions_first",
                "requested_language": params.get("language"),
            },
            "transcript": {
                "transcript_source": transcript.get("transcript_source"),
                "caption_track": transcript.get("caption_track"),
                "language": transcript.get("language"),
                "segment_count": len(segments),
                # Coverage is the transcript's own span, not the video's:
                # captions can stop early, and a silent tail is real.
                "first_segment_start_seconds": segments[0]["start"] if segments else None,
                "last_segment_end_seconds": segments[-1]["end"] if segments else None,
                "duration_seconds": transcript.get("duration"),
                "word_count": sum(len(s["text"].split()) for s in segments),
            },
            "files": [
                {"path": "transcript/transcript.json", "description": "Timed segments"},
                {"path": "transcript/transcript.txt", "description": "Plain text"},
                {"path": "transcript/subtitles.srt", "description": "SubRip subtitles"},
            ],
            "generated_at": now_utc_iso(),
        }
        ctx.storage.save_bytes(
            ctx.job_relative("manifest.json"), json.dumps(manifest, indent=2).encode()
        )
        ctx.shared["manifest"] = manifest
        ctx.info(
            f"manifest.json written ({manifest['transcript']['word_count']} words, "
            f"source={transcript.get('transcript_source')})"
        )
        ctx.set_step_progress(self.name, 100)
