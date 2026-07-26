"""Writes metadata/frames.json, the v2 analytics/content/events files, and
the root manifest.json.

manifest.json is the entry point for AI consumption: an LLM reading only this
file should understand the entire dataset without opening anything else first.
Dataset schema v2 adds statuses for everything that could not be extracted —
values are never fabricated and missing data is never coerced to 0.
"""
import json
from datetime import datetime, timezone

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext
from app.utils import status as st
from app.utils.audio_stats import compute_audio_stats
from app.utils.dataset_v2 import (
    DATASET_SCHEMA_VERSION,
    build_analysis_summary,
    build_engagement_breakdown,
    build_posting_context,
    normalize_events,
)


class GenerateMetadataStep(PipelineStep):
    name = "generating_metadata"
    label = "Generating metadata"
    consumes = ("video", "transcript", "frames")
    produces = ("manifest",)

    def run(self, ctx: PipelineContext) -> None:
        ctx.set_step_progress(self.name, 10)
        settings = get_settings()
        video = ctx.shared["video"]
        transcript = ctx.shared.get("transcript") or {
            "language": None,
            "skipped": True,
            "skipped_reason": "not_run",
        }
        frames = ctx.shared.get("frames") or []
        mode = ctx.shared["mode"]

        frames_json_bytes = json.dumps(frames, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("metadata", "frames.json"), frames_json_bytes)
        ctx.set_step_progress(self.name, 40)

        source_description = (
            "Video downloaded from the source URL"
            if ctx.shared.get("source_url")
            else "Original uploaded video file"
        )
        files = [
            {
                "path": f"source/{ctx.shared['stored_source_filename']}",
                "description": source_description,
                "size_bytes": ctx.storage.size_of(ctx.shared["source_relative_path"]),
            }
        ]

        if not transcript.get("skipped"):
            for fname, desc in [
                ("transcript.txt", "Plain-text transcript, one line per segment"),
                (
                    "transcript.json",
                    "Structured transcript: language, duration, and timestamped segments"
                    + (" with speaker labels" if any(s.get("speaker") for s in transcript.get("segments", [])) else ""),
                ),
                ("subtitles.srt", "SRT subtitle file derived from the transcript"),
            ]:
                rel = ctx.job_relative("transcript", fname)
                files.append(
                    {
                        "path": f"transcript/{fname}",
                        "description": desc,
                        "size_bytes": ctx.storage.size_of(rel),
                    }
                )
        else:
            files.append(
                {
                    "path": "transcript/transcript.json",
                    "description": f"Transcription skipped ({transcript.get('skipped_reason', 'unknown')})",
                    "size_bytes": ctx.storage.size_of(ctx.job_relative("transcript", "transcript.json")),
                }
            )

        for frame in frames:
            rel = ctx.job_relative("frames", frame["image"])
            desc = f"Extracted frame #{frame['frame']} at t={frame['timestamp']}s ({mode} mode)"
            if frame.get("scene_id") is not None:
                desc += f", scene {frame['scene_id']}"
            files.append(
                {"path": f"frames/{frame['image']}", "description": desc, "size_bytes": ctx.storage.size_of(rel)}
            )

        files.append(
            {
                "path": "metadata/frames.json",
                "description": "Machine-readable list of every extracted frame with timestamp and scene id",
                "size_bytes": len(frames_json_bytes),
            }
        )

        performance = ctx.shared.get("performance")
        if performance is not None:
            comments_rel = ctx.job_relative("performance", "comments.json")
            if ctx.storage.exists(comments_rel):
                files.append(
                    {
                        "path": "performance/comments.json",
                        "description": f"Top comments fetched from {performance.get('platform', 'source')} (best-effort, legacy v1 shape)",
                        "size_bytes": ctx.storage.size_of(comments_rel),
                    }
                )

        ctx.set_step_progress(self.name, 55)

        # ---- Dataset schema v2 outputs ----
        adaptive_count = ctx.shared.get("frame_count", len(frames))
        frame_counts = ctx.shared.get("frame_counts_by_category") or {"adaptive": adaptive_count}

        # comments/ (already written by fetch_source for URL jobs; record status here)
        comment_extraction = ctx.shared.get("comment_extraction")
        if comment_extraction is None and not ctx.shared.get("source_url"):
            # Direct file upload: there is no platform to extract comments from.
            comment_extraction = {
                "status": st.NOT_AVAILABLE,
                "reason": "Direct file upload has no source platform; upload a comments export manually",
                "error": None,
                "platform_comment_count": None,
                "extracted_comment_count": 0,
                "attempted_at": None,
            }
            ctx.storage.save_bytes(
                ctx.job_relative("comments", "extraction_status.json"),
                json.dumps(comment_extraction, indent=2).encode(),
            )
        for rel_path, description in (
            ("comments/top_comments.json", "Top comments with author/like/reply/pinned metadata (v2)"),
            ("comments/extraction_status.json", "Explicit comment-extraction outcome: status, reason, counts"),
        ):
            if ctx.storage.exists(ctx.job_relative(*rel_path.split("/"))):
                files.append(
                    {
                        "path": rel_path,
                        "description": description,
                        "size_bytes": ctx.storage.size_of(ctx.job_relative(*rel_path.split("/"))),
                    }
                )

        # content/caption.txt — the exact post caption when the source provided one
        caption = (performance or {}).get("description")
        content_status: dict = {}
        if caption:
            caption_bytes = str(caption).encode("utf-8")
            ctx.storage.save_bytes(ctx.job_relative("content", "caption.txt"), caption_bytes)
            files.append(
                {"path": "content/caption.txt", "description": "Exact post caption/description from the source", "size_bytes": len(caption_bytes)}
            )
            content_status["caption"] = {"status": st.SUCCESS, "source": st.SOURCE_AUTO}
        else:
            content_status["caption"] = {
                "status": st.MANUAL_REQUIRED,
                "source": None,
                "reason": "No caption in source metadata; add manually",
            }
        for slot in ("onscreen_text", "pinned_comment", "cover_text"):
            content_status[slot] = {
                "status": st.MANUAL_REQUIRED,
                "source": None,
                "reason": "Requires manual annotation (or a future OCR/AI pass)",
            }

        # content/audio.json — pacing analysis from the transcript
        audio_stats = compute_audio_stats(
            transcript if not transcript.get("skipped") else transcript,
            video.get("duration_seconds"),
            ctx.shared.get("music_info"),
        )
        audio_bytes = json.dumps(audio_stats, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("content", "audio.json"), audio_bytes)
        files.append(
            {
                "path": "content/audio.json",
                "description": "Audio/pacing analysis: word counts, overall vs active-narration WPM, silence periods, music info",
                "size_bytes": len(audio_bytes),
            }
        )

        # analytics/performance.json — full engagement breakdown with statuses + rates
        engagement = build_engagement_breakdown(performance)
        engagement_bytes = json.dumps(engagement, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("analytics", "performance.json"), engagement_bytes)
        files.append(
            {
                "path": "analytics/performance.json",
                "description": "Engagement metrics with per-field status/source and computed rates (rates only when views + numerator are valid)",
                "size_bytes": len(engagement_bytes),
            }
        )

        # events.json — manual-first timeline of visual/narrative progression
        events = normalize_events(ctx.options.get("events"))
        events_bytes = json.dumps(events, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("events.json"), events_bytes)
        files.append(
            {
                "path": "events.json",
                "description": "Timeline events (hook, first interaction, twists, CTA...); manual entries take priority over AI-detected ones",
                "size_bytes": len(events_bytes),
            }
        )

        ctx.set_step_progress(self.name, 75)

        manifest = {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "job_id": ctx.job_id,
            "app_version": settings.APP_VERSION,
            "original_filename": ctx.shared["original_filename"],
            "video": video,
            "source_video_sha256": ctx.shared.get("source_sha256"),
            "language": transcript.get("language"),
            "transcript_available": not transcript.get("skipped", False),
            "extraction_mode": mode,
            "extraction_params": {
                k: v
                for k, v in ctx.options.items()
                if k
                in (
                    "interval_ms",
                    "target_frames",
                    "frame_format",
                    "frame_max_dim",
                    "opening_dense_enabled",
                    "opening_dense_duration",
                    "opening_dense_interval",
                )
            },
            "frame_count": adaptive_count,
            "frame_counts": frame_counts,
            "files": files,
            "processing": {
                "started_at": ctx.shared.get("started_at"),
                "manifest_generated_at": datetime.now(timezone.utc).isoformat(),
                "last_rebuilt_at": None,
            },
            "extraction_report": ctx.shared.get("stage_reports") or [],
            "performance": performance,
            "posting_context": build_posting_context(performance, ctx.shared.get("source_url")),
            "content": content_status,
            "comments": {
                k: v for k, v in (comment_extraction or {}).items() if k != "comments"
            } or None,
            "events_count": len(events),
            "analysis_summary": build_analysis_summary(events, audio_stats),
            "analyses": {},
        }

        ctx.set_step_progress(self.name, 90)
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("manifest.json"), manifest_bytes)
        ctx.shared["manifest"] = manifest
        ctx.info("manifest.json written (dataset schema v2)")
        ctx.set_step_progress(self.name, 100)
