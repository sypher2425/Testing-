"""Queue a non-destructive processing benchmark from an existing local source.

Run inside the API container with PYTHONPATH=/app. Creates a new job and a
hardlink/copy of the immutable source; original job artifacts stay untouched.
"""
import argparse
import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from app.database import get_session
from app.models import Job
from app.schemas import CreateJobOptions
from app.storage import get_storage
from app.tasks import process_job


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-job")
    parser.add_argument("--profile", default="balanced", choices=["fast", "balanced", "detailed"])
    parser.add_argument("--target-frames", type=int, default=80)
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--vision", action="store_true")
    args = parser.parse_args()
    storage = get_storage()
    db = get_session()
    try:
        original = db.get(Job, args.source_job) if args.source_job else db.query(Job).filter(
            Job.duration_seconds.between(1700, 1800), Job.status == "completed",
            Job.original_filename.notlike("Upgrade benchmark%"),
        ).order_by(Job.created_at.desc()).first()
        if original is None:
            raise RuntimeError("No matching completed source job found")
        source = storage.get(f"{original.id}/source/{original.stored_source_filename}")
        if not source.is_file():
            raise RuntimeError("The selected source video is no longer available")
        job_id = str(uuid.uuid4())
        destination = storage.get(f"{job_id}/source/{original.stored_source_filename}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        options = CreateJobOptions(target_frames=args.target_frames, processing_profile=args.profile,
            ocr_enabled=args.ocr, vision_enabled=args.vision, source_preference="whisper_only").model_dump(mode="json")
        job = Job(id=job_id, original_filename=f"Upgrade benchmark ({args.profile}, {args.target_frames} frames)",
            stored_source_filename=original.stored_source_filename, job_type="video", status="queued",
            current_step="queued", mode="adaptive", options=options, source_url=None,
            source_sha256=original.source_sha256, file_size_bytes=source.stat().st_size,
            step_progress={}, last_heartbeat=datetime.now(timezone.utc))
        db.add(job)
        db.commit()
        process_job.apply_async(args=(job_id,), retry=False)
        print(json.dumps({"job_id": job_id, "original_job_id": original.id,
                          "original_duration_seconds": original.duration_seconds,
                          "options": options}))
    finally:
        db.close()


if __name__ == "__main__":
    main()
