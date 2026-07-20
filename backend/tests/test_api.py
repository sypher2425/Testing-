import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.ffmpeg import ProbeResult


@pytest.fixture()
def client():
    return TestClient(app)


FAKE_PROBE = ProbeResult(
    duration_seconds=5.0, width=320, height=240, fps=10.0, codec="h264", has_audio=True, raw={}
)


def test_get_unknown_job_returns_error_envelope(client):
    resp = client.get("/api/jobs/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert "message" in body["error"]


def test_create_job_rejects_bad_extension(client):
    resp = client.post(
        "/api/jobs",
        files={"file": ("clip.txt", io.BytesIO(b"not a video"), "text/plain")},
        data={"mode": "adaptive"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unprocessable"


def test_create_job_rejects_invalid_mode(client):
    resp = client.post(
        "/api/jobs",
        files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes"), "video/mp4")},
        data={"mode": "not-a-real-mode"},
    )
    assert resp.status_code == 422


def test_create_job_succeeds_and_enqueues(client):
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.delay"
    ) as mock_delay:
        resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "interval", "interval_ms": "500"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert job_id
    mock_delay.assert_called_once_with(job_id)

    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "queued"
    assert body["mode"] == "interval"


def test_create_job_rejects_corrupt_file(client):
    from app.utils.ffmpeg import FFmpegError

    with patch(
        "app.api.routes.jobs.ffprobe",
        side_effect=FFmpegError("bad", cmd=["ffprobe"], returncode=1, stderr="err"),
    ):
        resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"garbage"), "video/mp4")},
            data={"mode": "adaptive"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unprocessable"


def test_download_before_completion_is_rejected(client):
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.delay"
    ):
        create_resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "adaptive"},
        )
    job_id = create_resp.json()["job_id"]
    resp = client.get(f"/api/jobs/{job_id}/download?asset=zip")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_frame_path_traversal_is_rejected(client):
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.delay"
    ):
        create_resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "adaptive"},
        )
    job_id = create_resp.json()["job_id"]
    resp = client.get(f"/api/jobs/{job_id}/frames/..%2F..%2Fmanifest.json")
    assert resp.status_code in (400, 404)


def test_create_job_rejects_both_file_and_url(client):
    resp = client.post(
        "/api/jobs",
        files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes"), "video/mp4")},
        data={"mode": "adaptive", "url": "https://youtu.be/xyz"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_create_job_rejects_neither_file_nor_url(client):
    resp = client.post("/api/jobs", data={"mode": "adaptive"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_create_job_rejects_local_network_url(client):
    resp = client.post("/api/jobs", data={"mode": "adaptive", "url": "http://localhost:8000/x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_create_job_rejects_non_http_url(client):
    resp = client.post("/api/jobs", data={"mode": "adaptive", "url": "ftp://example.com/x"})
    assert resp.status_code == 400


def test_create_job_with_url_succeeds_and_defers_probe(client):
    with patch("app.tasks.process_job.delay") as mock_delay:
        resp = client.post(
            "/api/jobs",
            data={"mode": "adaptive", "url": "https://youtu.be/xyz", "manual_view_count": "500"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(job_id)

    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["source_url"] == "https://youtu.be/xyz"
    assert body["status"] == "queued"
    assert body["job_type"] == "video"


def test_create_research_job_succeeds_and_enqueues(client):
    with patch("app.tasks.process_job.delay") as mock_delay:
        resp = client.post(
            "/api/jobs/research",
            json={"query": "roblox animation tips", "result_count": 10, "sort_mode": "newest", "min_views": 1000},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(job_id)

    status_resp = client.get(f"/api/jobs/{job_id}")
    body = status_resp.json()
    assert body["job_type"] == "research"
    assert body["mode"] == "research"
    assert body["original_filename"] == "Research: roblox animation tips"
    assert body["options"]["research"]["sort_mode"] == "newest"


def test_create_research_job_rejects_bad_params(client):
    resp = client.post("/api/jobs/research", json={"query": "", "result_count": 10})
    assert resp.status_code == 422
    resp = client.post("/api/jobs/research", json={"query": "x", "result_count": 26})
    assert resp.status_code == 422
    resp = client.post("/api/jobs/research", json={"query": "x", "sort_mode": "views"})
    assert resp.status_code == 422


def test_research_job_download_rejects_non_zip_assets(client):
    with patch("app.tasks.process_job.delay"):
        resp = client.post("/api/jobs/research", json={"query": "test topic"})
    job_id = resp.json()["job_id"]

    from app.database import get_session
    from app.models import Job

    session = get_session()
    try:
        job = session.get(Job, job_id)
        job.status = "completed"
        session.commit()
    finally:
        session.close()

    resp = client.get(f"/api/jobs/{job_id}/download?asset=frames")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"
