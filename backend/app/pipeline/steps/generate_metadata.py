"""Writes metadata/frames.json and the root manifest.json.

manifest.json is the entry point for AI consumption: an LLM reading only this
file should understand the entire dataset without opening anything else first.
"""
import json
from datetime import datetime, timezone

from app.config import get_settings
from app.pipeline.base import PipelineStep
from app.pipeline.context import PipelineContext


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

        files = [
            {
                "path": f"source/{ctx.shared['stored_source_filename']}",
                "description": "Original uploaded video file",
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

        manifest = {
            "job_id": ctx.job_id,
            "app_version": settings.APP_VERSION,
            "original_filename": ctx.shared["original_filename"],
            "video": video,
            "language": transcript.get("language"),
            "transcript_available": not transcript.get("skipped", False),
            "extraction_mode": mode,
            "extraction_params": {
                k: v
                for k, v in ctx.options.items()
                if k in ("interval_ms", "target_frames", "frame_format", "frame_max_dim")
            },
            "frame_count": len(frames),
            "files": files,
            "processing": {
                "started_at": ctx.shared.get("started_at"),
                "manifest_generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "analyses": {},
        }

        ctx.set_step_progress(self.name, 80)
        manifest_bytes = json.dumps(manifest, indent=2).encode()
        ctx.storage.save_bytes(ctx.job_relative("manifest.json"), manifest_bytes)
        ctx.shared["manifest"] = manifest
        ctx.info("manifest.json written")
        ctx.set_step_progress(self.name, 100)
