"""Build a new analysis job from immutable inputs of a completed dataset."""
import json
import os
import shutil
import uuid

from app.pipeline.base import PipelineStep
from app.pipeline.errors import PipelineFailedError


class ReuseDatasetStep(PipelineStep):
    name = "fetching_source"
    label = "Reusing existing frames and transcript"
    produces = ("video", "frames", "transcript")

    def run(self, ctx):
        original_id = str(uuid.UUID(ctx.options["reuse_job_id"]))
        if original_id == ctx.job_id:
            raise PipelineFailedError("invalid_reuse", "A dataset cannot reuse itself")
        source = ctx.storage.get(original_id)
        destination = ctx.storage.get(ctx.job_id)
        if not (source / "manifest.json").exists():
            raise PipelineFailedError("source_missing", "The original dataset expired before analysis could begin")
        source = source.resolve()
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        # Only fixed application-owned directories. Source/frame hardlinks are
        # immutable; mutable metadata is copied, so the original cannot change.
        for folder in ("source", "frames", "transcript", "metadata", "content", "comments", "performance", "analytics"):
            root = source / folder
            if not root.exists():
                continue
            for file in root.rglob("*"):
                ctx.check_cancel()
                if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(source):
                    continue
                target = destination / file.relative_to(source)
                if not target.resolve().is_relative_to(destination):
                    raise PipelineFailedError("unsafe_reuse_path", "Invalid artifact path")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    continue
                if folder in {"source", "frames"}:
                    try:
                        os.link(file, target)
                        continue
                    except OSError:
                        pass
                shutil.copy2(file, target)
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        frames = json.loads((destination / "metadata" / "frames.json").read_text(encoding="utf-8"))
        transcript_path = destination / "transcript" / "transcript.json"
        transcript = json.loads(transcript_path.read_text(encoding="utf-8")) if transcript_path.exists() else {
            "language": None, "skipped": True, "skipped_reason": "not_available", "segments": []}
        ctx.shared.update({"video": manifest["video"], "frames": frames, "transcript": transcript,
            "language": transcript.get("language"), "frame_count": manifest.get("frame_count", len(frames)),
            "frame_counts_by_category": manifest.get("frame_counts") or {},
            "performance": manifest.get("performance"),
            "frame_decoding": manifest.get("frame_decoding"),
            "reused_from_job_id": original_id})
        params = manifest.get("extraction_params") or {}
        for shared_name, saved_name in {"frame_range": "frame_range", "visual_scan": "visual_scan",
                "adaptive_config": "adaptive_config", "burst_config": "burst_config",
                "interval_config": "interval", "opening_dense_config": "opening_dense",
                "key_events_config": "key_events"}.items():
            if saved_name in params:
                ctx.shared[shared_name] = params[saved_name]
        ctx.update_job({**{k:v for k,v in manifest["video"].items() if k in {
            "duration_seconds", "width", "height", "fps", "codec", "has_audio"}},
            "language": transcript.get("language"), "frame_count": len(frames)})
        # Mark reused stages complete while preserving the normal UI step order.
        for step in ("probing", "loading_model", "transcribing", "extracting_frames"):
            ctx.set_step_progress(step, 100)
        ctx.info("Existing source, frames and transcript reused; extraction and transcription skipped. Original dataset preserved.")
        ctx.set_step_progress(self.name, 100)
