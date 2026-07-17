import asyncio
import json
import os
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import db_session
from app.config import Settings, get_settings
from app.errors import AppError, bad_request, insufficient_storage, not_found, unprocessable
from app.models import TERMINAL_STATES, Job, JobLog
from app.schemas import CreateJobOptions, CreateJobResponse, FrameListResponse, JobListResponse, JobStatusResponse
from app.storage import get_storage
from app.storage.base import StorageBackend
from app.utils.disk import ensure_enough_disk
from app.utils.ffmpeg import FFmpegError, ffprobe
from app.utils.filenames import is_safe_relative_path, sanitize_filename

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi"}
CHUNK_SIZE = 1024 * 1024

_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "json": "application/json",
    "txt": "text/plain",
    "srt": "application/x-subrip",
    "zip": "application/zip",
    "mp4": "video/mp4",
}


def _content_type_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _to_status_response(job: Job) -> JobStatusResponse:
    return JobStatusResponse.model_validate(job.to_dict())


@router.post("", status_code=202, response_model=CreateJobResponse)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("adaptive"),
    interval_ms: int = Form(1000),
    target_frames: int = Form(80),
    frame_format: str = Form("jpeg"),
    frame_max_dim: int = Form(1280),
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> CreateJobResponse:
    try:
        options = CreateJobOptions(
            mode=mode,
            interval_ms=interval_ms,
            target_frames=target_frames,
            frame_format=frame_format,
            frame_max_dim=frame_max_dim,
        )
    except ValidationError as exc:
        raise unprocessable("Invalid job options", json.loads(exc.json())) from exc

    if not file.filename:
        raise bad_request("Uploaded file must have a filename")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise unprocessable(
            f"Unsupported file extension '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    content_length = request.headers.get("content-length")
    incoming_mb = (int(content_length) / (1024 * 1024)) if content_length else 0.0
    try:
        ensure_enough_disk(str(settings.data_path), incoming_mb=incoming_mb)
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    job_id = str(uuid.uuid4())
    stored_source_filename = f"video.{ext}"
    relative_source_path = f"{job_id}/source/{stored_source_filename}"
    dest_path = storage.get(relative_source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise unprocessable(
                        f"Upload exceeds MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)"
                    )
                out.write(chunk)
    except AppError:
        storage.delete(job_id)
        raise
    finally:
        await file.close()

    if total_bytes == 0:
        storage.delete(job_id)
        raise bad_request("Uploaded file is empty")

    try:
        ffprobe(str(dest_path), timeout=30)
    except FFmpegError as exc:
        storage.delete(job_id)
        raise unprocessable(
            "File failed video validation (ffprobe could not read it). "
            "It may be corrupt or not a real video file despite its extension.",
            exc.to_detail(),
        ) from exc

    job = Job(
        id=job_id,
        original_filename=sanitize_filename(file.filename),
        stored_source_filename=stored_source_filename,
        status="queued",
        current_step="queued",
        mode=options.mode.value,
        options=options.model_dump(mode="json"),
        file_size_bytes=total_bytes,
        step_progress={},
        last_heartbeat=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    from app.tasks import process_job

    process_job.delay(job_id)

    return CreateJobResponse(job_id=job_id)


@router.get("", response_model=JobListResponse)
def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(db_session),
) -> JobListResponse:
    total = db.query(func.count(Job.id)).scalar() or 0
    jobs = (
        db.query(Job)
        .order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return JobListResponse(
        jobs=[_to_status_response(j) for j in jobs], total=total, page=page, page_size=page_size
    )


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise not_found(f"Job {job_id} not found")
    return job


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, db: Session = Depends(db_session)) -> JobStatusResponse:
    job = _get_job_or_404(db, job_id)
    return _to_status_response(job)


@router.get("/{job_id}/events")
async def job_events(job_id: str, db: Session = Depends(db_session)) -> StreamingResponse:
    _get_job_or_404(db, job_id)

    async def event_stream():
        last_payload = None
        while True:
            db.expire_all()
            job = db.get(Job, job_id)
            if job is None:
                yield "event: error\ndata: {\"message\": \"job not found\"}\n\n"
                return
            payload = json.dumps(_to_status_response(job).model_dump(mode="json"))
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            if job.status in TERMINAL_STATES:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{job_id}/transcript")
def get_transcript(
    job_id: str,
    format: str = Query("json", pattern="^(txt|json|srt)$"),
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    job = _get_job_or_404(db, job_id)
    filename = {"txt": "transcript.txt", "json": "transcript.json", "srt": "subtitles.srt"}[format]
    rel = f"{job_id}/transcript/{filename}"
    if not storage.exists(rel):
        raise not_found(f"Transcript not available yet for job {job_id}")
    data = storage.get(rel).read_bytes()
    return Response(content=data, media_type=_content_type_for(filename))


@router.get("/{job_id}/video")
def get_source_video(
    job_id: str,
    request: Request,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    """Serves the original source video with HTTP Range support, so the
    Results view can play it back and seek for clickable-timestamp preview."""
    job = _get_job_or_404(db, job_id)
    rel = f"{job_id}/source/{job.stored_source_filename}"
    if not storage.exists(rel):
        raise not_found("Source video not found")
    path = storage.get(rel)
    file_size = path.stat().st_size
    content_type = _content_type_for(job.stored_source_filename) or "video/mp4"

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=content_type)

    try:
        range_value = range_header.strip().removeprefix("bytes=")
        start_str, _, end_str = range_value.partition("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
    except ValueError as exc:
        raise bad_request("Invalid Range header") from exc

    chunk_size = end - start + 1

    def iter_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = f.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        iter_range(),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_size),
        },
    )


@router.get("/{job_id}/logs")
def get_logs(
    job_id: str,
    since_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(db_session),
) -> dict:
    _get_job_or_404(db, job_id)
    rows = (
        db.query(JobLog)
        .filter(JobLog.job_id == job_id, JobLog.id > since_id)
        .order_by(JobLog.id.asc())
        .limit(limit)
        .all()
    )
    return {
        "logs": [
            {"id": r.id, "timestamp": r.timestamp.isoformat(), "level": r.level, "message": r.message}
            for r in rows
        ]
    }


@router.get("/{job_id}/manifest")
def get_manifest(
    job_id: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    _get_job_or_404(db, job_id)
    rel = f"{job_id}/manifest.json"
    if not storage.exists(rel):
        raise not_found(f"manifest.json not available yet for job {job_id}")
    data = storage.get(rel).read_bytes()
    return Response(content=data, media_type="application/json")


@router.get("/{job_id}/frames", response_model=FrameListResponse)
def list_frames(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> FrameListResponse:
    job = _get_job_or_404(db, job_id)
    rel = f"{job_id}/metadata/frames.json"
    if not storage.exists(rel):
        return FrameListResponse(frames=[], total=0, page=page, page_size=page_size)
    frames = json.loads(storage.get(rel).read_bytes())
    total = len(frames)
    start = (page - 1) * page_size
    page_items = frames[start : start + page_size]
    return FrameListResponse(frames=page_items, total=total, page=page, page_size=page_size)


@router.get("/{job_id}/frames/{filename}")
def get_frame(
    job_id: str,
    filename: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    _get_job_or_404(db, job_id)
    if not is_safe_relative_path(filename) or "/" in filename:
        raise bad_request("Invalid frame filename")
    rel = f"{job_id}/frames/{filename}"
    if not storage.exists(rel):
        raise not_found(f"Frame {filename} not found for job {job_id}")
    data = storage.get(rel).read_bytes()
    return Response(content=data, media_type=_content_type_for(filename))


@router.get("/{job_id}/download")
def download(
    job_id: str,
    asset: str = Query(..., pattern="^(zip|transcript|frames)$"),
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    job = _get_job_or_404(db, job_id)
    if job.status != "completed":
        raise bad_request(f"Job {job_id} is not completed yet (status={job.status})")

    if asset == "zip":
        rel = f"{job_id}/output.zip"
        if not storage.exists(rel):
            raise not_found("output.zip not found")
        path = storage.get(rel)
        return FileResponse(
            path, media_type="application/zip", filename=f"{job_id}-dataset.zip"
        )

    if asset == "transcript":
        return _zip_subset(job_id, storage, ["transcript"], f"{job_id}-transcript.zip")

    return _zip_subset(job_id, storage, ["frames", "metadata/frames.json"], f"{job_id}-frames.zip")


def _zip_subset(job_id: str, storage: StorageBackend, rel_entries: list[str], download_name: str) -> Response:
    job_root = storage.get(job_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in rel_entries:
            full = job_root / entry
            if full.is_dir():
                for root, _dirs, files in os.walk(full):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        arcname = os.path.relpath(fpath, job_root)
                        zf.write(fpath, arcname)
            elif full.is_file():
                zf.write(full, entry)
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=download_name,
        background=BackgroundTask(lambda: os.unlink(tmp.name)),
    )


@router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    job = _get_job_or_404(db, job_id)

    if job.status not in TERMINAL_STATES:
        job.cancel_requested = True
        db.commit()
        if job.celery_task_id:
            from app.celery_app import celery_app

            celery_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")

    storage.delete(job_id)
    db.query(JobLog).filter(JobLog.job_id == job_id).delete()
    db.delete(job)
    db.commit()
    return Response(status_code=204)
