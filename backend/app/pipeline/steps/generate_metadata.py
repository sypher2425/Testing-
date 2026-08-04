"""Writes metadata/frames.json, the v2 analytics/content/events files, and
the root manifest.json.

manifest.json is the entry point for AI consumption: an LLM reading only this
file should understand the entire dataset without opening anything else first.
Dataset schema v2 adds statuses for everything that could not be extracted —
values are never fabricated and missing data is never coerced to 0.
"""
import json

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
from app.utils.frame_schema import describe_frame
from app.utils.platform_capabilities import capabilities_for
from app.utils.timestamps import now_utc_iso
from app.utils.validation import validate_dataset


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

        # v2.2: every frame's description comes from its OWN metadata via the
        # shared describe_frame helper (the validator recomputes the same
        # string) — a dense frame is never again described as "adaptive".
        for frame in frames:
            frame.setdefault("description", describe_frame(frame))

        frames_json_bytes = json.dumps(frames, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("metadata", "frames.json"), frames_json_bytes)
        ctx.set_step_progress(self.name, 40)

        source_description = (
            "Video downloaded from the source URL"
            if ctx.shared.get("source_url")
            else "Original uploaded video file"
        )
        source_entry = {
            "path": f"source/{ctx.shared['stored_source_filename']}",
            "description": source_description,
            "size_bytes": ctx.storage.size_of(ctx.shared["source_relative_path"]),
        }
        # The manifest always describes the source video, but by default the
        # ZIP carries only the analysis — say so explicitly rather than listing
        # a file the archive doesn't contain.
        if not settings.ZIP_INCLUDE_SOURCE_VIDEO:
            source_entry["included_in_zip"] = False
            source_entry["reason"] = (
                "Excluded from output.zip by configuration (ZIP_INCLUDE_SOURCE_VIDEO=false); "
                "download it from /api/jobs/{job_id}/video"
            )
        files = [source_entry]

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
            files.append(
                {
                    "path": f"frames/{frame['image']}",
                    "description": frame["description"],
                    "size_bytes": ctx.storage.size_of(rel),
                }
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
            content_status["caption"] = {
                "status": st.SUCCESS,
                "source": st.SOURCE_YT_DLP,
                "entry_method": st.ENTRY_AUTOMATIC,
            }
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

        # R1.5: record EFFECTIVE extraction params (not the raw request),
        # with a source label. Keep the flat keys for v1 readers. Fallback
        # values come from the same env settings ExtractFramesStep would
        # have used, never the raw (possibly-null) user options.
        dense_config = ctx.shared.get("opening_dense_config")
        if dense_config is None:
            user_duration = ctx.options.get("opening_dense_duration")
            user_interval = ctx.options.get("opening_dense_interval")
            enabled = bool(ctx.options.get("opening_dense_enabled", True))
            dense_config = {
                "enabled": enabled,
                "duration_seconds": (user_duration if user_duration is not None else settings.OPENING_DENSE_DURATION) if enabled else None,
                "interval_seconds": (user_interval if user_interval is not None else settings.OPENING_DENSE_INTERVAL) if enabled else None,
                "source": "user_interface" if (user_duration is not None or user_interval is not None) else "default_configuration",
            }
        key_events_config = ctx.shared.get("key_events_config") or {
            "enabled": bool(events),
            "offsets_seconds": [-0.25, 0.0, 0.25],
            "event_count": len(events),
        }
        extraction_params: dict = {
            k: ctx.options.get(k)
            for k in ("interval_ms", "target_frames", "frame_format", "frame_max_dim")
            if k in ctx.options
        }
        extraction_params["mode"] = mode
        extraction_params["opening_dense"] = dense_config
        extraction_params["key_events"] = key_events_config
        # Interval modes may widen their interval to keep whole-video coverage
        # within MAX_FRAMES. Record what actually ran, not just what was asked
        # for, so a consumer never mistakes 0.35s sampling for the 0.2s
        # requested.
        interval_config = ctx.shared.get("interval_config")
        if interval_config:
            extraction_params["interval"] = interval_config
            extraction_params["interval_ms"] = round(
                interval_config["effective_interval_seconds"] * 1000
            )
            extraction_params["requested_interval_ms"] = round(
                interval_config["requested_interval_seconds"] * 1000
            )
        # v2.2: the nested opening_dense block is canonical. The flat keys are
        # deprecated but always written FROM the effective config, so they can
        # never be null while enabled and never contradict the nested values
        # (they used to echo the raw — possibly null — request).
        extraction_params["opening_dense_enabled"] = dense_config["enabled"]
        extraction_params["opening_dense_duration"] = dense_config["duration_seconds"]
        extraction_params["opening_dense_interval"] = dense_config["interval_seconds"]
        extraction_params["_deprecated"] = {
            "opening_dense_enabled": "Use extraction_params.opening_dense.enabled (canonical since schema v2.2)",
            "opening_dense_duration": "Use extraction_params.opening_dense.duration_seconds (canonical since schema v2.2)",
            "opening_dense_interval": "Use extraction_params.opening_dense.interval_seconds (canonical since schema v2.2)",
        }

        # R1.5: performance snapshot metadata + identity block.
        url_canonical = ctx.shared.get("url_canonical") or {}
        canonical_url = url_canonical.get("canonical")
        platform_post_id = url_canonical.get("platform_post_id")
        resolved_platform = (performance or {}).get("platform") or url_canonical.get("platform") or "unknown"
        performance_snapshot = None
        if performance and performance.get("source_url"):
            performance_snapshot = {
                "fetched_at": ctx.shared.get("performance_fetched_at"),
                "metric_window": "lifetime_at_capture",
                "source": "yt_dlp",
                "platform": resolved_platform,
            }
        elif performance:
            performance_snapshot = {
                "fetched_at": None,
                "metric_window": "unknown",
                "source": "manual",
                "platform": resolved_platform,
            }

        manifest_generated_at = now_utc_iso()
        identity_block = {
            "dataset_id": ctx.job_id,
            "platform": resolved_platform,
            "platform_post_id": platform_post_id,
            "canonical_url": canonical_url,
            "source_url_original": ctx.shared.get("source_url"),
            "source_video_sha256": ctx.shared.get("source_sha256"),
            "exported_at": manifest_generated_at,
        }

        total_frame_count = sum(frame_counts.values())

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
            "extraction_params": extraction_params,
            "frame_count": adaptive_count,
            "frame_count_legacy_meaning": "adaptive_frames_only",
            "total_frame_count": total_frame_count,
            "frame_counts": frame_counts,
            "files": files,
            "processing": {
                "started_at": ctx.shared.get("started_at"),
                "manifest_generated_at": manifest_generated_at,
                "last_rebuilt_at": None,
            },
            "extraction_report": ctx.shared.get("stage_reports") or [],
            "identity": identity_block,
            "performance": performance,
            "performance_snapshot": performance_snapshot,
            "platform_capabilities": capabilities_for(resolved_platform),
            "posting_context": build_posting_context(performance, ctx.shared.get("source_url")),
            "content": content_status,
            "comments": {
                k: v for k, v in (comment_extraction or {}).items() if k != "comments"
            } or None,
            "events_count": len(events),
            "analysis_summary": build_analysis_summary(events, audio_stats),
            "analyses": {},
        }

        # R1.5: run the validator BEFORE writing the manifest, so the
        # manifest can carry a summary of its own validation status. Frames
        # are injected via a private key that's stripped before serialization.
        manifest["_frames_for_validation"] = frames
        job_dir_path = ctx.storage.get(ctx.job_relative(""))
        report = validate_dataset(job_dir_path, manifest)
        del manifest["_frames_for_validation"]

        report_bytes = json.dumps(report.to_dict(), indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("metadata", "validation_report.json"), report_bytes)
        files.append(
            {
                "path": "metadata/validation_report.json",
                "description": "Pre-export validation status, warnings, and errors",
                "size_bytes": len(report_bytes),
            }
        )
        manifest["validation"] = {
            "status": report.status,
            "warnings_count": len(report.warnings),
            "errors_count": len(report.errors),
            "validated_at": report.validated_at,
        }
        for warning in report.warnings:
            ctx.warning(f"[validation] {warning['code']}: {warning['message']}")
        if report.errors:
            for err in report.errors:
                ctx.error(f"[validation] {err['code']}: {err['message']}")

        ctx.set_step_progress(self.name, 90)
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("manifest.json"), manifest_bytes)
        ctx.shared["manifest"] = manifest
        ctx.info(f"manifest.json written (dataset schema v{DATASET_SCHEMA_VERSION}, validation={report.status})")
        ctx.set_step_progress(self.name, 100)
