import asyncio
import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

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
from app.schemas import (
    CreateJobOptions,
    CreateJobResponse,
    CreateResearchJobRequest,
    FrameListResponse,
    JobListResponse,
    JobStatusResponse,
)
from app.storage import get_storage
from app.storage.base import StorageBackend
from app.utils.disk import ensure_enough_disk
from app.utils.ffmpeg import FFmpegError, ffprobe
from app.utils.filenames import is_safe_relative_path, sanitize_filename
from app.utils.manifest_compat import normalize_frames

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


def _queue_position(db: Session, job: Job) -> int | None:
    """Number of unfinished jobs created before this one. Only meaningful
    while the job is still waiting — a running or finished job returns None.
    With TRANSCRIPTION_CONCURRENCY=1 this is literally 'how many ahead of me'."""
    if job.status != "queued":
        return None
    return (
        db.query(func.count(Job.id))
        .filter(~Job.status.in_(TERMINAL_STATES), Job.created_at < job.created_at)
        .scalar()
        or 0
    )


def _to_status_response(job: Job, db: Session | None = None) -> JobStatusResponse:
    position = _queue_position(db, job) if db is not None else None
    return JobStatusResponse.model_validate(job.to_dict(queue_position=position))


_BLOCKED_HOSTNAME_PREFIXES = ("127.", "10.", "192.168.", "169.254.")
_BLOCKED_HOSTNAMES = {"localhost", "0.0.0.0"}


def _validate_ingest_url(url: str) -> None:
    """Defensive scheme/host check before handing a user-supplied URL to
    yt-dlp — restricts to public http(s) targets, not an internal/local one."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise bad_request("url must be a valid http:// or https:// URL")
    hostname = parsed.hostname.lower()
    if hostname in _BLOCKED_HOSTNAMES or any(hostname.startswith(p) for p in _BLOCKED_HOSTNAME_PREFIXES):
        raise bad_request("url must not point to a local/private network address")


def _build_performance_overrides(
    manual_title: str | None,
    manual_description: str | None,
    manual_uploader: str | None,
    manual_upload_date: str | None,
    manual_view_count: int | None,
    manual_like_count: int | None,
    manual_comment_count: int | None,
    manual_share_count: int | None,
    manual_hashtags: str | None,
) -> dict:
    return {
        "title": manual_title,
        "description": manual_description,
        "uploader": manual_uploader,
        "upload_date": manual_upload_date,
        "view_count": manual_view_count,
        "like_count": manual_like_count,
        "comment_count": manual_comment_count,
        "share_count": manual_share_count,
        "hashtags": [h.strip() for h in manual_hashtags.split(",") if h.strip()] if manual_hashtags else [],
    }


@router.post("", status_code=202, response_model=CreateJobResponse)
async def create_job(
    request: Request,
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    mode: str = Form("adaptive"),
    interval_ms: int = Form(1000),
    target_frames: int = Form(80),
    frame_format: str = Form("jpeg"),
    frame_max_dim: int = Form(1280),
    manual_title: str | None = Form(None),
    manual_description: str | None = Form(None),
    manual_uploader: str | None = Form(None),
    manual_upload_date: str | None = Form(None),
    manual_view_count: int | None = Form(None),
    manual_like_count: int | None = Form(None),
    manual_comment_count: int | None = Form(None),
    manual_share_count: int | None = Form(None),
    manual_hashtags: str | None = Form(None),
    opening_dense_enabled: bool = Form(True),
    opening_dense_duration: float | None = Form(None),
    opening_dense_interval: float | None = Form(None),
    events: str | None = Form(None),
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
            opening_dense_enabled=opening_dense_enabled,
            opening_dense_duration=opening_dense_duration,
            opening_dense_interval=opening_dense_interval,
        )
    except ValidationError as exc:
        raise unprocessable("Invalid job options", json.loads(exc.json())) from exc

    parsed_events: list = []
    if events:
        try:
            parsed_events = json.loads(events)
            if not isinstance(parsed_events, list):
                raise ValueError("events must be a JSON array")
        except ValueError as exc:
            raise unprocessable(f"Invalid events JSON: {exc}") from exc

    has_file = file is not None and bool(file.filename)
    has_url = bool(url and url.strip())
    if has_file and has_url:
        raise bad_request("Provide either a file upload or a url, not both")
    if not has_file and not has_url:
        raise bad_request("Either a file upload or a url must be provided")

    performance_overrides = _build_performance_overrides(
        manual_title,
        manual_description,
        manual_uploader,
        manual_upload_date,
        manual_view_count,
        manual_like_count,
        manual_comment_count,
        manual_share_count,
        manual_hashtags,
    )
    options_dict = options.model_dump(mode="json")
    options_dict["performance_overrides"] = performance_overrides
    options_dict["events"] = parsed_events

    job_id = str(uuid.uuid4())

    if has_url:
        assert url is not None
        url = url.strip()
        _validate_ingest_url(url)
        try:
            ensure_enough_disk(str(settings.data_path), incoming_mb=0)
        except ValueError as exc:
            raise insufficient_storage(str(exc)) from exc

        job = Job(
            id=job_id,
            original_filename=url[:512],
            stored_source_filename="video",
            status="queued",
            current_step="queued",
            mode=options.mode.value,
            options=options_dict,
            source_url=url,
            step_progress={},
            last_heartbeat=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()

    else:
        assert file is not None and file.filename
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

        stored_source_filename = f"video.{ext}"
        relative_source_path = f"{job_id}/source/{stored_source_filename}"
        dest_path = storage.get(relative_source_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
        total_bytes = 0
        hasher = hashlib.sha256()
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
                    hasher.update(chunk)
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

        source_sha256 = hasher.hexdigest()
        duplicate = (
            db.query(Job).filter(Job.source_sha256 == source_sha256).order_by(Job.created_at.desc()).first()
        )

        job = Job(
            id=job_id,
            original_filename=sanitize_filename(file.filename),
            stored_source_filename=stored_source_filename,
            status="queued",
            current_step="queued",
            mode=options.mode.value,
            options=options_dict,
            file_size_bytes=total_bytes,
            source_sha256=source_sha256,
            step_progress={},
            last_heartbeat=datetime.now(timezone.utc),
        )
        db.add(job)
        if duplicate is not None:
            db.add(
                JobLog(
                    job_id=job_id,
                    level="warning",
                    message=(
                        f"This source video is byte-identical to job {duplicate.id} "
                        f"({duplicate.original_filename!r}) — you may be processing the same dataset twice."
                    ),
                )
            )
        db.commit()

    from app.tasks import process_job

    process_job.delay(job_id)

    return CreateJobResponse(job_id=job_id)


@router.post("/research", status_code=202, response_model=CreateJobResponse)
def create_research_job(
    body: CreateResearchJobRequest,
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> CreateJobResponse:
    try:
        ensure_enough_disk(str(settings.data_path), incoming_mb=0)
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        job_type="research",
        original_filename=f"Research: {body.query.strip()}"[:512],
        stored_source_filename="",
        status="queued",
        current_step="queued",
        mode="research",
        options={"research": body.model_dump(mode="json")},
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
        jobs=[_to_status_response(j, db) for j in jobs], total=total, page=page, page_size=page_size
    )


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise not_found(f"Job {job_id} not found")
    return job


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, db: Session = Depends(db_session)) -> JobStatusResponse:
    job = _get_job_or_404(db, job_id)
    return _to_status_response(job, db)


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
            payload = json.dumps(_to_status_response(job, db).model_dump(mode="json"))
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
    job = _get_job_or_404(db, job_id)
    manifest_name = "research-manifest.json" if job.job_type == "research" else "manifest.json"
    rel = f"{job_id}/{manifest_name}"
    if not storage.exists(rel):
        raise not_found(f"{manifest_name} not available yet for job {job_id}")
    data = storage.get(rel).read_bytes()
    return Response(content=data, media_type="application/json")


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.get("/{job_id}/research-transcript/{video_id}")
def get_research_transcript(
    job_id: str,
    video_id: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    _get_job_or_404(db, job_id)
    if not _VIDEO_ID_RE.match(video_id):
        raise bad_request("Invalid video id")
    rel = f"{job_id}/transcripts/{video_id}.txt"
    if not storage.exists(rel):
        raise not_found(f"No transcript for video {video_id}")
    data = storage.get(rel).read_bytes()
    return Response(content=data, media_type="text/plain; charset=utf-8")


@router.get("/{job_id}/frames", response_model=FrameListResponse)
def list_frames(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
    category: str = Query("adaptive", pattern="^(adaptive|opening_dense|key_event|all)$"),
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> FrameListResponse:
    job = _get_job_or_404(db, job_id)
    rel = f"{job_id}/metadata/frames.json"
    if not storage.exists(rel):
        return FrameListResponse(frames=[], total=0, page=page, page_size=page_size)
    # normalize_frames backfills what v1 datasets lack (category — every
    # frame was adaptive — mode, and a description) without touching disk.
    frames = normalize_frames(json.loads(storage.get(rel).read_bytes()))
    if category != "all":
        frames = [f for f in frames if f.get("category") == category]
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


_FRAME_SUBDIRS = {"opening_dense", "key_events"}


@router.get("/{job_id}/frames/{subdir}/{filename}")
def get_frame_in_subdir(
    job_id: str,
    subdir: str,
    filename: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    """Serves the v2 frame groups (frames/opening_dense/, frames/key_events/)."""
    _get_job_or_404(db, job_id)
    if subdir not in _FRAME_SUBDIRS:
        raise bad_request(f"Unknown frame group '{subdir}'")
    if not is_safe_relative_path(filename) or "/" in filename:
        raise bad_request("Invalid frame filename")
    rel = f"{job_id}/frames/{subdir}/{filename}"
    if not storage.exists(rel):
        raise not_found(f"Frame {subdir}/{filename} not found for job {job_id}")
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

    if job.job_type == "research" and asset != "zip":
        raise bad_request("Research jobs only support asset=zip")

    if asset == "zip":
        rel = f"{job_id}/output.zip"
        if not storage.exists(rel):
            raise not_found("output.zip not found")
        path = storage.get(rel)
        if job.job_type == "research":
            query = ((job.options or {}).get("research") or {}).get("query", "research")
            stamp = (job.completed_at or job.created_at).date().isoformat()
            download_name = f"research-{_slugify(query)}-{stamp}.zip"
        else:
            download_name = f"{job_id}-dataset.zip"
        return FileResponse(path, media_type="application/zip", filename=download_name)

    if asset == "transcript":
        return _zip_subset(job_id, storage, ["transcript"], f"{job_id}-transcript.zip")

    return _zip_subset(job_id, storage, ["frames", "metadata/frames.json"], f"{job_id}-frames.zip")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "query"


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
