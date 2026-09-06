"""Large-upload path: streaming endpoint, size enforcement, and cleanup.

The streaming route exists because Starlette buffers multipart bodies to a
temp file before the handler runs — unusable at tens of GB. These tests pin
the behavior that makes a 60GB cap real: reject from Content-Length before
transferring, reject mid-stream when the client doesn't declare a size, and
never leave a partial file behind.
"""
import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.utils.ffmpeg import ProbeResult

FAKE_PROBE = ProbeResult(
    duration_seconds=5.0, width=320, height=240, fps=10.0, codec="h264", has_audio=True, raw={}
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def data_dir():
    """Where job directories actually live (storage root), not DATA_DIR."""
    path = get_settings().jobs_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_dirs(data_dir):
    return {p.name for p in data_dir.iterdir() if p.is_dir()}


def test_streaming_upload_creates_job(client, data_dir):
    import hashlib

    payload = b"fake mp4 bytes" * 500
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ) as mock_delay:
        resp = client.post(
            "/api/jobs/upload?filename=clip.mp4&mode=adaptive",
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(args=(job_id,), retry=False)

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "queued"
    assert status["file_size_bytes"] == len(payload)
    assert status["original_filename"] == "clip.mp4"

    stored = data_dir / job_id / "source" / "video.mp4"
    assert stored.is_file()
    assert stored.read_bytes() == payload

    from app.database import get_session
    from app.models import Job

    session = get_session()
    try:
        assert session.get(Job, job_id).source_sha256 == hashlib.sha256(payload).hexdigest()
    finally:
        session.close()


def test_streaming_upload_rejects_bad_extension(client):
    resp = client.post(
        "/api/jobs/upload?filename=notes.txt",
        content=b"whatever",
        headers={"content-type": "application/octet-stream"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unprocessable"


def test_oversized_content_length_rejected_before_any_write(client, data_dir):
    """The whole point of the early check: a too-large upload must 413 without
    the client having to transfer the body first, and without creating a job dir."""
    before = _job_dirs(data_dir)
    settings = get_settings()
    too_big = settings.MAX_UPLOAD_MB * 1024 * 1024 + 1

    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE):
        resp = client.post(
            "/api/jobs/upload?filename=huge.mp4",
            content=b"x" * 16,  # tiny real body; the *declared* size is what matters
            headers={"content-type": "application/octet-stream", "content-length": str(too_big)},
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert _job_dirs(data_dir) == before


def test_oversized_stream_without_content_length_rejected_midstream(client, data_dir, monkeypatch):
    """Chunked bodies declare no size, so the mid-stream check is the only
    guard — and it must clean up the partial file it already wrote."""
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    try:
        before = _job_dirs(data_dir)

        def chunked_body():
            for _ in range(4):
                yield b"y" * (512 * 1024)  # 2MB total, cap is 1MB

        with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE):
            resp = client.post(
                "/api/jobs/upload?filename=big.mp4",
                content=chunked_body(),
                headers={"content-type": "application/octet-stream"},
            )
        assert resp.status_code == 413
        assert _job_dirs(data_dir) == before  # partial write removed
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_empty_upload_rejected_and_cleaned_up(client, data_dir):
    before = _job_dirs(data_dir)
    resp = client.post(
        "/api/jobs/upload?filename=empty.mp4",
        content=b"",
        headers={"content-type": "application/octet-stream"},
    )
    assert resp.status_code == 400
    assert _job_dirs(data_dir) == before


def test_write_failure_leaves_no_orphan_job_dir(client, data_dir):
    """An ENOSPC (or any OSError) mid-write must not strand a partial file with
    no job row — the old handler only cleaned up on AppError."""
    before = _job_dirs(data_dir)

    real_open = open

    def exploding_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        if "wb" in args or kwargs.get("mode") == "wb":
            original_write = handle.write

            def boom(data):
                original_write(data)
                raise OSError(28, "No space left on device")

            handle.write = boom  # type: ignore[method-assign]
        return handle

    with patch("builtins.open", side_effect=exploding_open):
        with pytest.raises(OSError):
            client.post(
                "/api/jobs/upload?filename=clip.mp4",
                content=b"some bytes",
                headers={"content-type": "application/octet-stream"},
            )
    assert _job_dirs(data_dir) == before


def test_failed_probe_removes_job_dir(client, data_dir):
    from app.utils.ffmpeg import FFmpegError

    before = _job_dirs(data_dir)
    with patch(
        "app.api.routes.jobs.ffprobe",
        side_effect=FFmpegError("bad", cmd=["ffprobe"], returncode=1, stderr="err"),
    ):
        resp = client.post(
            "/api/jobs/upload?filename=clip.mp4",
            content=b"not really a video",
            headers={"content-type": "application/octet-stream"},
        )
    assert resp.status_code == 422
    assert _job_dirs(data_dir) == before


def test_multipart_route_still_works_and_rejects_by_content_length(client):
    """The multipart route stays for URL jobs and small files, and now returns
    a real 413 instead of a 422 for oversized declarations."""
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ):
        resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "adaptive"},
        )
    assert resp.status_code == 202

    settings = get_settings()
    resp = client.post(
        "/api/jobs",
        files={"file": ("clip.mp4", io.BytesIO(b"x" * 32), "video/mp4")},
        data={"mode": "adaptive"},
        headers={"content-length": str(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)},
    )
    assert resp.status_code == 413


def test_default_cap_is_60gb():
    settings = get_settings()
    assert settings.MAX_UPLOAD_MB == 61440
    assert settings.MAX_UPLOAD_MB * 1024 * 1024 == 64424509440


def test_file_size_bytes_holds_a_60gb_value():
    """BigInteger, not Integer — 60GB overflows a 32-bit column on Postgres."""
    from app.database import get_session
    from app.models import Job

    sixty_gb = 60 * 1024 * 1024 * 1024
    session = get_session()
    try:
        job = Job(
            original_filename="huge.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            file_size_bytes=sixty_gb,
        )
        session.add(job)
        session.commit()
        job_id = job.id
        session.expire_all()
        assert session.get(Job, job_id).file_size_bytes == sixty_gb
    finally:
        session.close()


# ---------------------------------------------------------------- disk guard


def test_disk_guard_applies_headroom_multiplier(monkeypatch, tmp_path):
    """A source video costs more than its own size (frames, audio, zip), so
    the guard must require a multiple of the incoming size."""
    from app.utils import disk

    monkeypatch.setattr(disk, "free_disk_mb", lambda path: 1200.0)
    monkeypatch.setenv("MIN_FREE_DISK_MB", "100")
    monkeypatch.setenv("UPLOAD_DISK_HEADROOM_MULTIPLIER", "1.5")
    get_settings.cache_clear()
    try:
        # 1000MB upload x1.5 = 1500MB + 100 margin > 1200 available -> reject.
        with pytest.raises(ValueError) as exc:
            disk.ensure_enough_disk(str(tmp_path), incoming_mb=1000)
        assert "derived artifacts" in str(exc.value)

        # Same free space, smaller file: 600 x1.5 = 900 + 100 = 1000 <= 1200.
        disk.ensure_enough_disk(str(tmp_path), incoming_mb=600)

        # multiplier=1.0 checks the raw size only.
        disk.ensure_enough_disk(str(tmp_path), incoming_mb=1000, multiplier=1.0)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_disk_guard_message_omits_multiplier_for_zero_size(monkeypatch, tmp_path):
    from app.utils import disk

    monkeypatch.setattr(disk, "free_disk_mb", lambda path: 10.0)
    monkeypatch.setenv("MIN_FREE_DISK_MB", "100")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError) as exc:
            disk.ensure_enough_disk(str(tmp_path), incoming_mb=0)
        assert "derived artifacts" not in str(exc.value)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
