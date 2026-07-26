"""Research Mode pipeline: YouTube topic search → transcript bundle.

Never downloads video files — metadata and captions only. Reuses the shared
yt-dlp infrastructure (pinned version, extractor self-update retry, optional
COOKIES_FILE) and the generic ZipOutputStep for packaging.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone

from app.utils.timestamps import now_utc_iso
from pathlib import Path

from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.utils.captions import subtitles_to_text
from app.utils.ytdlp import (
    YtDlpError,
    cookies_status,
    download_captions,
    extract_metadata,
    search_videos,
)

# Over-fetch factor: search wider than requested, filter, then keep the top N.
OVERFETCH_FACTOR = 2
OVERFETCH_CAP = 50

_PUBLIC_FIELDS = ("id", "title", "channel", "views", "likes", "upload_date", "duration", "url")


def _has_english(langs: dict | None) -> bool:
    return any(k == "en" or k.startswith(("en-", "en_")) for k in (langs or {}))


def apply_filters(
    videos: list[dict],
    *,
    min_views: int | None,
    uploaded_within_days: int | None,
    max_duration_seconds: int | None,
    now: datetime | None = None,
) -> list[dict]:
    """Filter policy for missing values: when a filter is active and the video
    lacks the value needed to check it (unknown views/date), the video is
    excluded — an unverifiable candidate shouldn't pass an explicit filter.
    Unknown duration is kept (rare, and duration is the least critical)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=uploaded_within_days)).date() if uploaded_within_days else None

    kept = []
    for v in videos:
        if min_views is not None and (v.get("views") is None or v["views"] < min_views):
            continue
        if cutoff is not None:
            raw_date = v.get("upload_date")
            if not raw_date:
                continue
            try:
                upload_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if upload_date < cutoff:
                continue
        if max_duration_seconds is not None and v.get("duration") is not None and v["duration"] > max_duration_seconds:
            continue
        kept.append(v)
    return kept


class ResearchSearchStep(PipelineStep):
    name = "searching"
    label = "Searching YouTube"
    produces = ("research_videos",)

    def run(self, ctx: PipelineContext) -> None:
        params = ctx.options.get("research") or {}
        query = params["query"]
        count = int(params.get("result_count", 15))
        sort_mode = params.get("sort_mode", "top")
        min_views = params.get("min_views")
        within_days = params.get("uploaded_within_days")
        max_duration = params.get("max_duration_seconds")

        ctx.set_step_progress(self.name, 0)
        ctx.info(f"yt-dlp cookies: {cookies_status()}")
        ctx.info(f"Searching YouTube for {query!r} ({sort_mode} mode, target {count} videos)…")

        overfetch = min(count * OVERFETCH_FACTOR, OVERFETCH_CAP)
        try:
            entries = search_videos(query, overfetch, sort_mode, log=ctx.log)
        except YtDlpError as exc:
            raise PipelineFailedError(exc.code, f"YouTube search failed: {exc.message}", exc.to_detail()) from exc

        ctx.info(f"Found {len(entries)} candidate videos")
        ctx.set_step_progress(self.name, 10)

        videos: list[dict] = []
        total = max(len(entries), 1)
        for i, entry in enumerate(entries):
            ctx.check_cancel()
            video_id = entry.get("id")
            if not video_id:
                continue
            url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"

            # Cheap prefilter on flat-search fields to skip pointless full
            # metadata fetches; missing flat values pass through to the
            # authoritative filter after the full fetch.
            if max_duration is not None and entry.get("duration") and entry["duration"] > max_duration:
                ctx.set_step_progress(self.name, 10 + round(80 * (i + 1) / total))
                continue
            if min_views is not None and entry.get("view_count") is not None and entry["view_count"] < min_views:
                ctx.set_step_progress(self.name, 10 + round(80 * (i + 1) / total))
                continue

            try:
                meta = extract_metadata(url, log=ctx.log, log_cookie_status=False)
            except YtDlpError as exc:
                ctx.warning(f"Skipping {entry.get('title') or video_id!r}: {exc.message}")
                ctx.set_step_progress(self.name, 10 + round(80 * (i + 1) / total))
                continue

            videos.append(
                {
                    "id": meta.raw.get("id") or video_id,
                    "title": meta.title,
                    "channel": meta.uploader,
                    "views": meta.view_count,
                    "likes": meta.like_count,
                    "upload_date": meta.upload_date,
                    "duration": meta.duration,
                    "url": meta.raw.get("webpage_url") or url,
                    "has_manual_en": _has_english(meta.raw.get("subtitles")),
                    "has_auto_en": _has_english(meta.raw.get("automatic_captions")),
                }
            )
            ctx.set_step_progress(self.name, 10 + round(80 * (i + 1) / total))

        filtered = apply_filters(
            videos,
            min_views=min_views,
            uploaded_within_days=within_days,
            max_duration_seconds=max_duration,
        )
        ctx.info(f"Filtering… {len(filtered)} of {len(videos)} candidates remain")

        if sort_mode == "top":
            filtered.sort(key=lambda v: v.get("views") or 0, reverse=True)
        else:
            filtered.sort(key=lambda v: v.get("upload_date") or "", reverse=True)

        retained = filtered[:count]
        if not retained:
            raise PipelineFailedError(
                "no_results",
                f"No videos matched the search and filters for {query!r}. Try loosening the filters.",
            )

        ctx.info(f"Proceeding with {len(retained)} videos")
        ctx.shared["research_videos"] = retained
        ctx.set_step_progress(self.name, 100)


class ResearchCaptionsStep(PipelineStep):
    name = "fetching_captions"
    label = "Fetching captions"
    consumes = ("research_videos",)
    produces = ("research_results",)

    def run(self, ctx: PipelineContext) -> None:
        videos = ctx.shared.get("research_videos") or []
        ctx.set_step_progress(self.name, 0)

        results: list[dict] = []
        saved = 0
        total = max(len(videos), 1)
        for i, video in enumerate(videos, start=1):
            ctx.check_cancel()
            label = f"[{i}/{len(videos)}]"
            public = {k: video.get(k) for k in _PUBLIC_FIELDS}

            if not video.get("has_manual_en") and not video.get("has_auto_en"):
                ctx.warning(f"{label} {video.get('title')!r}: no English captions available — skipped")
                results.append({**public, "skipped_reason": "No captions available"})
                ctx.set_step_progress(self.name, round(100 * i / total))
                continue

            source = "manual" if video.get("has_manual_en") else "auto"
            ctx.info(f"{label} Downloading {source} captions for {video.get('title')!r}")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    sub_path = download_captions(
                        video["url"], Path(tmp), video["id"], manual=(source == "manual"), log=ctx.log
                    )
                    if sub_path is None:
                        raise ValueError("yt-dlp produced no caption file")
                    text = subtitles_to_text(sub_path.read_text(encoding="utf-8", errors="replace"))

                if not text.strip():
                    ctx.warning(f"{label} captions were empty after cleaning — skipped")
                    results.append({**public, "skipped_reason": "Captions were empty"})
                else:
                    ctx.storage.save_bytes(
                        ctx.job_relative("transcripts", f"{video['id']}.txt"), text.encode("utf-8")
                    )
                    results.append(
                        {
                            **public,
                            "transcript_file": f"transcripts/{video['id']}.txt",
                            "caption_source": source,
                        }
                    )
                    saved += 1
                    ctx.info(f"{label} ✓ Transcript saved")
            except YtDlpError as exc:
                ctx.warning(f"{label} caption fetch failed: {exc.message} — skipped")
                results.append({**public, "skipped_reason": exc.message or exc.code})
            except Exception as exc:  # noqa: BLE001 - one bad video must never kill the job
                ctx.warning(f"{label} failed: {exc} — skipped")
                results.append({**public, "skipped_reason": str(exc)})

            ctx.set_step_progress(self.name, round(100 * i / total))

        ctx.info(f"Transcripts saved for {saved} of {len(videos)} videos")
        ctx.shared["research_results"] = results
        ctx.set_step_progress(self.name, 100)


class ResearchManifestStep(PipelineStep):
    name = "generating_metadata"
    label = "Generating research manifest"
    consumes = ("research_results",)
    produces = ("research_manifest",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 10)
        params = ctx.options.get("research") or {}
        results = ctx.shared.get("research_results") or []

        manifest = {
            "query": params.get("query"),
            "mode": params.get("sort_mode", "top"),
            "filters": {
                "min_views": params.get("min_views"),
                "uploaded_within_days": params.get("uploaded_within_days"),
                "max_duration_seconds": params.get("max_duration_seconds"),
            },
            "result_count_requested": params.get("result_count", 15),
            "fetched_at": now_utc_iso(),
            "videos": results,
        }
        ctx.storage.save_bytes(
            ctx.job_relative("research-manifest.json"), json.dumps(manifest, indent=2).encode()
        )
        ctx.shared["research_manifest"] = manifest
        ctx.info("research-manifest.json written")
        ctx.set_step_progress(self.name, 100)
