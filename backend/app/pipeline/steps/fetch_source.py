"""First pipeline step: resolves the job's source video.

For a plain file upload, the file is already in place (saved by the upload
route) — this step is a fast no-op, except it still builds the performance
block if the user supplied manual overrides with no URL at all.

For a URL-ingested job, this step fetches metadata via yt-dlp, downloads the
video into source/, and best-effort fetches comments. A failure to get the
*video itself* (video_unavailable / extractor_outdated) fails the job — there
is nothing to process. A failure to get metadata/comments never does; it
just falls back to manual fields (or nulls) and logs a warning.
"""
import hashlib
import json
from pathlib import Path

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.disk import ensure_enough_disk
from app.utils.ytdlp import (
    VideoMetadata,
    YtDlpError,
    download_video,
    extract_comments,
    extract_metadata,
    fetch_profile_reel_view_count,
)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FetchSourceStep(PipelineStep):
    name = "fetching_source"
    label = "Fetching source video"
    produces = ("video_source", "performance")

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 0)
        source_url = ctx.shared.get("source_url")
        manual = ctx.options.get("performance_overrides") or {}

        if not source_url:
            if any(v not in (None, "", []) for v in manual.values()):
                performance = self._merge(
                    VideoMetadata(
                        source_url=None,
                        platform="manual",
                        title=None,
                        description=None,
                        uploader=None,
                        uploader_id=None,
                        upload_date=None,
                        view_count=None,
                        like_count=None,
                        comment_count=None,
                        share_count=None,
                        hashtags=[],
                        duration=None,
                        ext=None,
                        filesize_approx=None,
                    ),
                    manual,
                    comment_count_fallback=None,
                )
                ctx.shared["performance"] = performance
                ctx.update_job({"performance": performance})
            ctx.set_step_progress(self.name, 100)
            return

        settings = get_settings()

        try:
            metadata = extract_metadata(source_url, log=ctx.log)
        except YtDlpError as exc:
            self._log_ytdlp_failure(ctx, "metadata fetch", exc)
            raise PipelineFailedError(exc.code, exc.message, exc.to_detail()) from exc

        ctx.info(f"Resolved source URL via yt-dlp: platform={metadata.platform}, title={metadata.title!r}")
        missing = [
            field
            for field in ("view_count", "like_count", "comment_count", "share_count")
            if getattr(metadata, field) is None
        ]
        if missing:
            ctx.warning(
                f"yt-dlp did not return a value for: {', '.join(missing)} (platform={metadata.platform}). "
                "This is expected for share_count on Instagram (no such public metric exists there), "
                "and view_count is sometimes withheld even with cookies configured depending on content type."
            )
        ctx.set_step_progress(self.name, 20)

        if metadata.filesize_approx:
            try:
                ensure_enough_disk(
                    str(settings.data_path), incoming_mb=metadata.filesize_approx / (1024 * 1024)
                )
            except ValueError as exc:
                raise PipelineFailedError("insufficient_disk_space", str(exc)) from exc

        ctx.check_cancel()
        source_dir = ctx.storage.get(ctx.job_relative("source"))
        try:
            downloaded_path = download_video(source_url, source_dir, log=ctx.log)
        except YtDlpError as exc:
            self._log_ytdlp_failure(ctx, "video download", exc)
            raise PipelineFailedError(exc.code, exc.message, exc.to_detail()) from exc

        ctx.set_step_progress(self.name, 70)

        stored_filename = downloaded_path.name
        ctx.shared["stored_source_filename"] = stored_filename
        ctx.shared["source_relative_path"] = ctx.job_relative("source", stored_filename)

        update_fields = {
            "stored_source_filename": stored_filename,
            "file_size_bytes": downloaded_path.stat().st_size,
            "source_url": source_url,
        }
        title = manual.get("title") or metadata.title
        if title:
            update_fields["original_filename"] = title
            ctx.shared["original_filename"] = title
        ctx.update_job(update_fields)

        source_hash = _sha256_of(downloaded_path)
        ctx.shared["source_sha256"] = source_hash
        ctx.update_job({"source_sha256": source_hash})

        ctx.shared["music_info"] = {
            "track": metadata.raw.get("track"),
            "artist": metadata.raw.get("artist"),
        }

        ctx.set_step_progress(self.name, 80)
        ctx.check_cancel()

        extraction = extract_comments(
            source_url,
            limit=settings.YTDLP_COMMENT_LIMIT,
            log=ctx.log,
            platform=metadata.platform,
            platform_comment_count=metadata.comment_count,
        )
        comments = extraction["comments"]

        # v2 outputs: full comments + an explicit extraction outcome.
        ctx.storage.save_bytes(
            ctx.job_relative("comments", "top_comments.json"),
            json.dumps(comments, indent=2).encode(),
        )
        ctx.storage.save_bytes(
            ctx.job_relative("comments", "extraction_status.json"),
            json.dumps({k: v for k, v in extraction.items() if k != "comments"}, indent=2).encode(),
        )
        # Legacy v1 output kept for backward compatibility (same simple shape).
        legacy_comments = [
            {"author": c["author"], "text": c["text"], "like_count": c["like_count"], "timestamp": c["timestamp"]}
            for c in comments
        ]
        ctx.storage.save_bytes(
            ctx.job_relative("performance", "comments.json"),
            json.dumps(legacy_comments, indent=2).encode(),
        )
        ctx.shared["comment_extraction"] = extraction
        if comments:
            ctx.info(f"Fetched {len(comments)} top comments")
        else:
            ctx.warning(
                f"No comments extracted (status={extraction['status']}"
                + (f", reason={extraction['reason']}" if extraction.get("reason") else "")
                + ")"
            )

        performance = self._merge(metadata, manual, comment_count_fallback=len(comments) or None)

        if (
            metadata.platform == "instagram"
            and performance.get("view_count") is None
            and not manual.get("view_count")
            and metadata.uploader_id
            and metadata.raw.get("id")
        ):
            fallback_views = fetch_profile_reel_view_count(
                metadata.uploader_id, str(metadata.raw["id"]), log=ctx.log
            )
            if fallback_views is not None:
                performance["view_count"] = fallback_views
                performance["fields_from"]["view_count"] = "auto"
                performance["fields_status"]["view_count"] = {
                    "status": "success",
                    "source": "auto",
                    "reason": "Backfilled from the account's Reels grid",
                }

        ctx.shared["performance"] = performance
        ctx.update_job({"performance": performance})

        ctx.set_step_progress(self.name, 100)

    @staticmethod
    def _log_ytdlp_failure(ctx: PipelineContext, stage: str, exc: YtDlpError) -> None:
        ctx.error(f"yt-dlp {stage} failed [{exc.code}]: {exc.message}")
        if exc.stderr:
            tail = exc.stderr.strip()[-1500:]
            ctx.error(f"yt-dlp stderr (last 1500 chars): {tail}")

    # Metrics a platform genuinely does not expose publicly — different from
    # an extraction failure, and updating yt-dlp or adding cookies won't help.
    _METRIC_NOT_AVAILABLE: dict = {
        ("instagram", "share_count"): "Instagram has no public share-count metric",
        ("youtube", "share_count"): "YouTube does not expose a share count",
    }

    @classmethod
    def _merge(cls, metadata: VideoMetadata, manual: dict, *, comment_count_fallback: int | None) -> dict:
        fields_from: dict[str, str] = {}

        def pick(key: str, auto_value):
            manual_value = manual.get(key)
            if manual_value not in (None, "", []):
                fields_from[key] = "manual"
                return manual_value
            if auto_value not in (None, ""):
                fields_from[key] = "auto"
            return auto_value

        merged = {
            "source_url": metadata.source_url,
            "platform": metadata.platform,
            "title": pick("title", metadata.title),
            "description": pick("description", metadata.description),
            "uploader": pick("uploader", metadata.uploader),
            "upload_date": pick("upload_date", metadata.upload_date),
            "view_count": pick("view_count", metadata.view_count),
            "like_count": pick("like_count", metadata.like_count),
            "comment_count": pick("comment_count", metadata.comment_count or comment_count_fallback),
            "share_count": pick("share_count", metadata.share_count),
            "hashtags": manual.get("hashtags") or metadata.hashtags,
            "fields_from": fields_from,
        }

        # v2 additive: explain every null metric instead of leaving it bare.
        fields_status: dict[str, dict] = {}
        for key in ("view_count", "like_count", "comment_count", "share_count"):
            if merged[key] is not None:
                fields_status[key] = {"status": "success", "source": fields_from.get(key, "auto")}
            elif (metadata.platform, key) in cls._METRIC_NOT_AVAILABLE:
                fields_status[key] = {
                    "status": "not_available",
                    "source": None,
                    "reason": cls._METRIC_NOT_AVAILABLE[(metadata.platform, key)],
                }
            elif metadata.platform == "manual":
                fields_status[key] = {
                    "status": "manual_required",
                    "source": None,
                    "reason": "No source URL was provided; enter this value manually",
                }
            else:
                fields_status[key] = {
                    "status": "extraction_failed",
                    "source": None,
                    "reason": "The platform did not expose this field to the extractor",
                }
        merged["fields_status"] = fields_status
        return merged
