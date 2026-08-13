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

from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import db_session
from app.config import Settings, get_settings
from app.errors import (
    bad_request,
    insufficient_storage,
    not_found,
    payload_too_large,
    service_unavailable,
    unprocessable,
)
from app.models import TERMINAL_STATES, Job, JobLog
from app.schemas import (
    CreateJobOptions,
    CreateJobResponse,
    CreateResearchJobRequest,
    CreateTranscriptJobRequest,
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
# Transcript mode only needs an audio track, so it accepts audio containers
# the full dataset pipeline has no use for (there are no frames to extract
# from an .mp3).
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "wma"}
TRANSCRIBABLE_EXTENSIONS = ALLOWED_EXTENSIONS | AUDIO_EXTENSIONS

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


def _parse_options(**kwargs) -> CreateJobOptions:
    try:
        return CreateJobOptions(**kwargs)
    except ValidationError as exc:
        errors = json.loads(exc.json())
        # Name the offending field in the message itself. The structured detail
        # was always there, but the UI shows only the message, so "Invalid job
        # options" was all anyone ever saw.
        summary = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ())) or 'options'}: {e.get('msg')}"
            f" (got {e.get('input')!r})"
            for e in errors[:3]
        )
        raise unprocessable(f"Invalid job options — {summary}", errors) from exc


def _parse_events(events: str | None) -> list:
    if not events:
        return []
    try:
        parsed = json.loads(events)
        if not isinstance(parsed, list):
            raise ValueError("events must be a JSON array")
    except ValueError as exc:
        raise unprocessable(f"Invalid events JSON: {exc}") from exc
    return parsed


def _validated_extension(filename: str, allowed: set[str] = ALLOWED_EXTENSIONS) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed:
        raise unprocessable(
            f"Unsupported file extension '.{ext}'. Allowed: {', '.join(sorted(allowed))}",
        )
    return ext


def _enqueue(db: Session, job_id: str) -> None:
    """Hand the job to the broker, failing fast and cleanly if it is down.

    Celery's default retry policy spends ~20 seconds reconnecting before it
    gives up, so an unreachable Redis turned submission into a long hang and
    then a 500 -- while leaving a `queued` row behind that nothing would ever
    run. Here the job is deleted and the caller gets a message naming the
    actual problem.
    """
    from kombu.exceptions import OperationalError

    from app.tasks import process_job

    try:
        process_job.delay(job_id, retry=False)
    except (OperationalError, OSError) as exc:
        db.query(Job).filter(Job.id == job_id).delete()
        db.commit()
        get_storage().delete(job_id)
        raise service_unavailable(
            "The job queue is unreachable, so this job was not started. Check that the "
            "redis container is running (`docker compose ps`).",
            {"error": str(exc)},
        ) from exc


def _persist_video_job(
    db: Session,
    *,
    job_id: str,
    original_filename: str,
    stored_source_filename: str,
    mode: str,
    options_dict: dict,
    total_bytes: int,
    source_sha256: str,
) -> None:
    """Creates the job row (warning about a byte-identical earlier job) and
    queues it. Shared by the multipart and streaming upload routes so
    duplicate detection and enqueueing live in exactly one place."""
    duplicate = (
        db.query(Job).filter(Job.source_sha256 == source_sha256).order_by(Job.created_at.desc()).first()
    )
    job = Job(
        id=job_id,
        original_filename=original_filename,
        stored_source_filename=stored_source_filename,
        status="queued",
        current_step="queued",
        mode=mode,
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

    _enqueue(db, job_id)


async def _validate_uploaded_media(
    dest_path,
    storage: StorageBackend,
    job_id: str,
    settings: Settings,
    *,
    kind: str = "video",
    require_video: bool = True,
) -> None:
    """ffprobe the stored file, cleaning up the job dir if it isn't media.
    Run in a worker thread: ffprobe is a blocking subprocess and a large file
    on a slow volume would otherwise stall the event loop (and the container
    healthcheck) for the duration."""
    try:
        await to_thread.run_sync(
            lambda: ffprobe(
                str(dest_path),
                timeout=settings.FFPROBE_TIMEOUT_SECONDS,
                require_video=require_video,
            )
        )
    except FFmpegError as exc:
        storage.delete(job_id)
        raise unprocessable(
            f"File failed {kind} validation (ffprobe could not read it). "
            f"It may be corrupt or not a real {kind} file despite its extension.",
            exc.to_detail(),
        ) from exc


@router.post("/upload", status_code=202, response_model=CreateJobResponse)
async def create_job_from_stream(
    request: Request,
    filename: str = Query(..., description="Original filename; supplies the extension and display name"),
    mode: str = Query("adaptive"),
    interval_ms: int = Query(1000),
    target_frames: int = Query(80),
    frame_format: str = Query("jpeg"),
    frame_max_dim: int = Query(1280),
    opening_dense_enabled: bool = Query(True),
    opening_dense_duration: float | None = Query(None),
    opening_dense_interval: float | None = Query(None),
    storyboard_enabled: bool = Query(True),
    storyboard_columns: int | None = Query(None),
    storyboard_tiles_per_sheet: int | None = Query(None),
    storyboard_include_captions: bool = Query(True),
    events: str | None = Query(None),
    manual_title: str | None = Query(None),
    manual_description: str | None = Query(None),
    manual_uploader: str | None = Query(None),
    manual_upload_date: str | None = Query(None),
    manual_view_count: int | None = Query(None),
    manual_like_count: int | None = Query(None),
    manual_comment_count: int | None = Query(None),
    manual_share_count: int | None = Query(None),
    manual_hashtags: str | None = Query(None),
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> CreateJobResponse:
    """Streaming upload for large videos: the raw request body is written
    straight to /data in chunks.

    The multipart route (POST /api/jobs) is fine for small files, but Starlette
    buffers each multipart part to a temp file before the handler runs — so a
    60GB upload would need a second full copy on the container's own
    filesystem, which the disk guard cannot even see. Here nothing is buffered:
    body -> disk, hashing as we go.
    """
    options = _parse_options(
        mode=mode,
        interval_ms=interval_ms,
        target_frames=target_frames,
        frame_format=frame_format,
        frame_max_dim=frame_max_dim,
        opening_dense_enabled=opening_dense_enabled,
        opening_dense_duration=opening_dense_duration,
        opening_dense_interval=opening_dense_interval,
        storyboard_enabled=storyboard_enabled,
        storyboard_columns=storyboard_columns,
        storyboard_tiles_per_sheet=storyboard_tiles_per_sheet,
        storyboard_include_captions=storyboard_include_captions,
    )
    parsed_events = _parse_events(events)
    if not filename.strip():
        raise bad_request("A filename query parameter is required")
    ext = _validated_extension(filename)

    options_dict = options.model_dump(mode="json")
    options_dict["performance_overrides"] = _build_performance_overrides(
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
    options_dict["events"] = parsed_events

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    raw_length = request.headers.get("content-length")
    declared_bytes = int(raw_length) if raw_length and raw_length.isdigit() else None

    # Reject before reading a single byte when the client tells us the size.
    # Discovering the limit after transferring 60GB is useless to everyone.
    if declared_bytes is not None and declared_bytes > max_bytes:
        raise payload_too_large(
            f"Upload is {declared_bytes / (1024 * 1024):.0f}MB, which exceeds "
            f"MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)."
        )

    incoming_mb = (declared_bytes / (1024 * 1024)) if declared_bytes else 0.0
    try:
        ensure_enough_disk(str(settings.data_path), incoming_mb=incoming_mb)
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    job_id = str(uuid.uuid4())
    stored_source_filename = f"video.{ext}"
    dest_path = storage.get(f"{job_id}/source/{stored_source_filename}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    total_bytes = 0

    def write_chunk(out, chunk: bytes) -> None:
        hasher.update(chunk)
        out.write(chunk)

    try:
        with open(dest_path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise payload_too_large(
                        f"Upload exceeds MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)"
                    )
                await to_thread.run_sync(write_chunk, out, chunk)
    except BaseException:
        # Covers AppError, OSError/ENOSPC, and a client disconnecting
        # mid-upload — any of which would otherwise strand a partial file.
        storage.delete(job_id)
        raise

    if total_bytes == 0:
        storage.delete(job_id)
        raise bad_request("Uploaded file is empty")

    await _validate_uploaded_media(dest_path, storage, job_id, settings)

    _persist_video_job(
        db,
        job_id=job_id,
        original_filename=sanitize_filename(filename),
        stored_source_filename=stored_source_filename,
        mode=options.mode.value,
        options_dict=options_dict,
        total_bytes=total_bytes,
        source_sha256=hasher.hexdigest(),
    )
    return CreateJobResponse(job_id=job_id)


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
    storyboard_enabled: bool = Form(True),
    storyboard_columns: int | None = Form(None),
    storyboard_tiles_per_sheet: int | None = Form(None),
    storyboard_include_captions: bool = Form(True),
    events: str | None = Form(None),
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> CreateJobResponse:
    options = _parse_options(
        mode=mode,
        interval_ms=interval_ms,
        target_frames=target_frames,
        frame_format=frame_format,
        frame_max_dim=frame_max_dim,
        opening_dense_enabled=opening_dense_enabled,
        opening_dense_duration=opening_dense_duration,
        opening_dense_interval=opening_dense_interval,
        storyboard_enabled=storyboard_enabled,
        storyboard_columns=storyboard_columns,
        storyboard_tiles_per_sheet=storyboard_tiles_per_sheet,
        storyboard_include_captions=storyboard_include_captions,
    )
    parsed_events = _parse_events(events)

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

        _enqueue(db, job_id)
        return CreateJobResponse(job_id=job_id)

    assert file is not None and file.filename
    ext = _validated_extension(file.filename)

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    raw_length = request.headers.get("content-length")
    declared_bytes = int(raw_length) if raw_length and raw_length.isdigit() else None
    if declared_bytes is not None and declared_bytes > max_bytes:
        raise payload_too_large(
            f"Upload is {declared_bytes / (1024 * 1024):.0f}MB, which exceeds "
            f"MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)."
        )

    incoming_mb = (declared_bytes / (1024 * 1024)) if declared_bytes else 0.0
    try:
        ensure_enough_disk(str(settings.data_path), incoming_mb=incoming_mb)
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    stored_source_filename = f"video.{ext}"
    dest_path = storage.get(f"{job_id}/source/{stored_source_filename}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    hasher = hashlib.sha256()

    def write_chunk(out, chunk: bytes) -> None:
        hasher.update(chunk)
        out.write(chunk)

    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(settings.UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise payload_too_large(
                        f"Upload exceeds MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)"
                    )
                await to_thread.run_sync(write_chunk, out, chunk)
    except BaseException:
        # AppError, OSError/ENOSPC, or a client disconnect — all of which
        # would otherwise leave a partial file behind with no job row.
        storage.delete(job_id)
        raise
    finally:
        await file.close()

    if total_bytes == 0:
        storage.delete(job_id)
        raise bad_request("Uploaded file is empty")

    await _validate_uploaded_media(dest_path, storage, job_id, settings)

    _persist_video_job(
        db,
        job_id=job_id,
        original_filename=sanitize_filename(file.filename),
        stored_source_filename=stored_source_filename,
        mode=options.mode.value,
        options_dict=options_dict,
        total_bytes=total_bytes,
        source_sha256=hasher.hexdigest(),
    )
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

    _enqueue(db, job_id)

    return CreateJobResponse(job_id=job_id)


@router.post("/transcript", status_code=202, response_model=CreateJobResponse)
def create_transcript_job(
    body: CreateTranscriptJobRequest,
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> CreateJobResponse:
    """Transcript-only job from a link on any platform yt-dlp supports.

    Cheap by construction: platform captions are tried first, and Whisper
    only runs when there are none — so the disk check is a floor check, not
    a video-sized reservation."""
    url = body.url.strip()
    _validate_ingest_url(url)

    try:
        ensure_enough_disk(str(settings.data_path), incoming_mb=0)
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        job_type="transcript",
        # Replaced with the real title once yt-dlp resolves the link.
        original_filename=f"Transcript: {url}"[:512],
        stored_source_filename="",
        status="queued",
        current_step="queued",
        mode="transcript",
        source_url=url,
        options={"transcript": {**body.model_dump(mode="json"), "url": url}},
        step_progress={},
        last_heartbeat=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    _enqueue(db, job_id)

    return CreateJobResponse(job_id=job_id)


@router.post("/transcript/upload", status_code=202, response_model=CreateJobResponse)
async def create_transcript_job_from_stream(
    request: Request,
    filename: str = Query(..., description="Original filename; supplies the extension and display name"),
    language: str | None = Query(None, max_length=8),
    db: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
    storage: StorageBackend = Depends(get_storage),
) -> CreateJobResponse:
    """Transcribe a file you already have — the answer when a platform can't
    be extracted at all: save the video yourself and drop it in.

    Streams the raw body straight to disk like /api/jobs/upload (nothing is
    buffered), then runs the transcript pipeline. Audio-only files are
    accepted too: there are no frames to extract here, so an .mp3 is a
    perfectly good input.
    """
    if not filename.strip():
        raise bad_request("A filename query parameter is required")
    ext = _validated_extension(filename, TRANSCRIBABLE_EXTENSIONS)

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    raw_length = request.headers.get("content-length")
    declared_bytes = int(raw_length) if raw_length and raw_length.isdigit() else None
    if declared_bytes is not None and declared_bytes > max_bytes:
        raise payload_too_large(
            f"Upload is {declared_bytes / (1024 * 1024):.0f}MB, which exceeds "
            f"MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)."
        )

    try:
        ensure_enough_disk(
            str(settings.data_path), incoming_mb=(declared_bytes / (1024 * 1024)) if declared_bytes else 0.0
        )
    except ValueError as exc:
        raise insufficient_storage(str(exc)) from exc

    job_id = str(uuid.uuid4())
    stored_source_filename = f"source.{ext}"
    dest_path = storage.get(f"{job_id}/source/{stored_source_filename}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    total_bytes = 0

    def write_chunk(out, chunk: bytes) -> None:
        hasher.update(chunk)
        out.write(chunk)

    try:
        with open(dest_path, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise payload_too_large(
                        f"Upload exceeds MAX_UPLOAD_MB ({settings.MAX_UPLOAD_MB}MB)"
                    )
                await to_thread.run_sync(write_chunk, out, chunk)
    except BaseException:
        storage.delete(job_id)
        raise

    if total_bytes == 0:
        storage.delete(job_id)
        raise bad_request("Uploaded file is empty")

    # ffprobe here rather than in the worker so a file that isn't media at all
    # is rejected at submit time, with the job dir cleaned up.
    await _validate_uploaded_media(
        dest_path, storage, job_id, settings, kind="media", require_video=False
    )

    job = Job(
        id=job_id,
        job_type="transcript",
        original_filename=sanitize_filename(filename),
        stored_source_filename=stored_source_filename,
        status="queued",
        current_step="queued",
        mode="transcript",
        options={"transcript": {"url": None, "source_preference": "whisper_only", "language": language}},
        file_size_bytes=total_bytes,
        source_sha256=hasher.hexdigest(),
        step_progress={},
        last_heartbeat=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()

    _enqueue(db, job_id)

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


@router.get("/{job_id}/storyboards")
def list_storyboards(
    job_id: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    """The storyboard manifest: every sheet plus the tile→source-frame map.

    Returns an empty-but-valid manifest rather than a 404 when the job has no
    storyboards, so the UI can render an explanatory empty state.
    """
    _get_job_or_404(db, job_id)
    rel = f"{job_id}/storyboard_manifest.json"
    if not storage.exists(rel):
        return Response(
            content=json.dumps({"status": "not_available", "storyboards": [], "types_built": []}),
            media_type="application/json",
        )
    return Response(content=storage.get(rel).read_bytes(), media_type="application/json")


@router.get("/{job_id}/storyboards/{filename}")
def get_storyboard(
    job_id: str,
    filename: str,
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    _get_job_or_404(db, job_id)
    if not is_safe_relative_path(filename) or "/" in filename:
        raise bad_request("Invalid storyboard filename")
    rel = f"{job_id}/storyboards/{filename}"
    if not storage.exists(rel):
        raise not_found(f"Storyboard {filename} not found for job {job_id}")
    return Response(
        content=storage.get(rel).read_bytes(), media_type=_content_type_for(filename)
    )


@router.post("/{job_id}/storyboards/regenerate", status_code=202)
def regenerate_storyboards_route(
    job_id: str,
    db: Session = Depends(db_session),
) -> dict:
    """Rebuild the sheets from the frames already on disk — no re-extraction,
    no re-transcription. Useful after changing layout settings, or to retry
    when storyboard generation failed on a job whose frames are fine."""
    job = _get_job_or_404(db, job_id)
    if job.job_type in ("research", "transcript"):
        raise bad_request(f"{job.job_type.capitalize()} jobs have no storyboards")
    if job.status != "completed":
        raise bad_request(
            f"Job {job_id} is not completed yet (status={job.status}); storyboards can only be "
            "regenerated for a finished job."
        )

    from app.tasks import regenerate_storyboards

    regenerate_storyboards.delay(job_id)
    return {"job_id": job_id, "status": "queued"}


@router.get("/{job_id}/download")
def download(
    job_id: str,
    asset: str = Query(..., pattern="^(zip|transcript|frames|storyboards)$"),
    db: Session = Depends(db_session),
    storage: StorageBackend = Depends(get_storage),
) -> Response:
    job = _get_job_or_404(db, job_id)
    if job.status != "completed":
        raise bad_request(f"Job {job_id} is not completed yet (status={job.status})")

    if job.job_type == "research" and asset != "zip":
        raise bad_request("Research jobs only support asset=zip")
    if job.job_type == "transcript" and asset not in ("zip", "transcript"):
        raise bad_request("Transcript jobs only support asset=zip or asset=transcript")

    if asset == "zip":
        rel = f"{job_id}/output.zip"
        if not storage.exists(rel):
            raise not_found("output.zip not found")
        path = storage.get(rel)
        if job.job_type == "research":
            query = ((job.options or {}).get("research") or {}).get("query", "research")
            stamp = (job.completed_at or job.created_at).date().isoformat()
            download_name = f"research-{_slugify(query)}-{stamp}.zip"
        elif job.job_type == "transcript":
            stamp = (job.completed_at or job.created_at).date().isoformat()
            download_name = f"transcript-{_slugify(job.original_filename)}-{stamp}.zip"
        else:
            download_name = f"{job_id}-dataset.zip"
        return FileResponse(path, media_type="application/zip", filename=download_name)

    if asset == "transcript":
        return _zip_subset(job_id, storage, ["transcript"], f"{job_id}-transcript.zip")

    if asset == "storyboards":
        return _zip_subset(
            job_id,
            storage,
            ["storyboards", "storyboard_manifest.json"],
            f"{job_id}-storyboards.zip",
        )

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
