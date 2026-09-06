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


def test_streaming_upload_cors_accepts_private_lan_frontend(client):
    resp = client.options(
        "/api/jobs/upload?filename=clip.mp4",
        headers={
            "Origin": "http://192.168.1.25:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://192.168.1.25:3000"


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
    assert "mode" in resp.json()["error"]["message"]


def test_create_job_names_the_invalid_numeric_option(client):
    resp = client.post(
        "/api/jobs",
        files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes"), "video/mp4")},
        data={"mode": "adaptive", "target_frames": "0"},
    )
    assert resp.status_code == 422
    assert "target_frames" in resp.json()["error"]["message"]


def test_create_job_succeeds_and_enqueues(client):
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ) as mock_delay:
        resp = client.post(
            "/api/jobs",
            files={"file": ("clip.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "interval", "interval_ms": "500"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert job_id
    mock_delay.assert_called_once_with(args=(job_id,), retry=False)

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
        "app.tasks.process_job.apply_async"
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
        "app.tasks.process_job.apply_async"
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
    with patch("app.tasks.process_job.apply_async") as mock_delay:
        resp = client.post(
            "/api/jobs",
            data={"mode": "adaptive", "url": "https://youtu.be/xyz", "manual_view_count": "500"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(args=(job_id,), retry=False)

    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["source_url"] == "https://youtu.be/xyz"
    assert body["status"] == "queued"
    assert body["job_type"] == "video"


def test_create_research_job_succeeds_and_enqueues(client):
    with patch("app.tasks.process_job.apply_async") as mock_delay:
        resp = client.post(
            "/api/jobs/research",
            json={"query": "roblox animation tips", "result_count": 10, "sort_mode": "newest", "min_views": 1000},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(args=(job_id,), retry=False)

    status_resp = client.get(f"/api/jobs/{job_id}")
    body = status_resp.json()
    assert body["job_type"] == "research"
    assert body["mode"] == "research"
    assert body["original_filename"] == "Research: roblox animation tips"
    assert body["options"]["research"]["sort_mode"] == "newest"


def test_queue_position_counts_only_older_unfinished_jobs(client):
    """With concurrency 1, a second submitted job must report that it's
    waiting behind the first rather than looking frozen at 0%."""
    from app.database import get_session
    from app.models import Job

    # Other tests in this module leave queued jobs behind; queue position is
    # global by definition, so start from a clean table.
    cleanup = get_session()
    try:
        cleanup.query(Job).delete()
        cleanup.commit()
    finally:
        cleanup.close()

    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ):
        first = client.post(
            "/api/jobs",
            files={"file": ("a.mp4", io.BytesIO(b"fake mp4 bytes" * 100), "video/mp4")},
            data={"mode": "adaptive"},
        ).json()["job_id"]
        second = client.post(
            "/api/jobs",
            files={"file": ("b.mp4", io.BytesIO(b"other mp4 bytes" * 100), "video/mp4")},
            data={"mode": "adaptive"},
        ).json()["job_id"]

    assert client.get(f"/api/jobs/{first}").json()["queue_position"] == 0
    assert client.get(f"/api/jobs/{second}").json()["queue_position"] == 1

    # Once the first job finishes it stops blocking the queue.
    session = get_session()
    try:
        session.get(Job, first).status = "completed"
        session.commit()
    finally:
        session.close()

    assert client.get(f"/api/jobs/{second}").json()["queue_position"] == 0
    # A finished job isn't "waiting" at all.
    assert client.get(f"/api/jobs/{first}").json()["queue_position"] is None


def test_create_research_job_rejects_bad_params(client):
    resp = client.post("/api/jobs/research", json={"query": "", "result_count": 10})
    assert resp.status_code == 422
    resp = client.post("/api/jobs/research", json={"query": "x", "result_count": 26})
    assert resp.status_code == 422
    resp = client.post("/api/jobs/research", json={"query": "x", "sort_mode": "views"})
    assert resp.status_code == 422


def test_research_job_download_rejects_non_zip_assets(client):
    with patch("app.tasks.process_job.apply_async"):
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


def test_both_loopback_spellings_are_allowed_by_default():
    """A browser treats localhost and 127.0.0.1 as different origins, so
    listing only one turns an address-bar habit into an opaque "Failed to
    fetch" with nothing in the API log."""
    from app.config import Settings

    origins = Settings().cors_origins_list
    assert "http://localhost:3000" in origins
    assert "http://127.0.0.1:3000" in origins


def test_cors_preflight_succeeds_for_both_loopback_origins(client):
    for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
        resp = client.options(
            "/api/jobs",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert resp.status_code == 200, origin
        assert resp.headers.get("access-control-allow-origin") == origin


# --------------------------------------------------- real frontend payloads


@pytest.mark.parametrize(
    "label,query",
    [
        ("untouched defaults", "mode=adaptive&interval_ms=1000&target_frames=80&frame_format=jpeg&frame_max_dim=1280"),
        ("dense 0.2s preset", "mode=interval&interval_ms=200&target_frames=80&frame_format=jpeg&frame_max_dim=1280"),
        ("per_second", "mode=per_second&interval_ms=1000&target_frames=80&frame_format=png&frame_max_dim=720"),
        (
            "storyboard options set",
            "mode=adaptive&interval_ms=1000&target_frames=80&frame_format=jpeg&frame_max_dim=1280"
            "&storyboard_enabled=true&storyboard_columns=5&storyboard_tiles_per_sheet=24"
            "&storyboard_include_captions=true",
        ),
        (
            "storyboards off",
            "mode=adaptive&interval_ms=1000&target_frames=80&frame_format=jpeg&frame_max_dim=1280"
            "&storyboard_enabled=false&storyboard_include_captions=false",
        ),
    ],
)
def test_query_strings_the_frontend_actually_builds_are_accepted(client, label, query):
    """These are copied from lib/api.ts, not invented here. Every earlier test
    posted hand-written params, so a form state the UI can genuinely produce
    was never exercised against the schema."""
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ):
        resp = client.post(
            f"/api/jobs/upload?filename=a.mp4&{query}",
            content=b"fake mp4 bytes" * 50,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 202, f"{label}: {resp.text}"


def test_rejected_options_name_the_offending_field(client):
    """A cleared number box used to post 0 and come back as a bare "Invalid
    job options", which says nothing about which box to fix."""
    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async"
    ):
        resp = client.post(
            "/api/jobs/upload?filename=a.mp4&mode=adaptive&interval_ms=1000"
            "&target_frames=0&frame_format=jpeg&frame_max_dim=1280",
            content=b"fake mp4 bytes" * 50,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 422
    message = resp.json()["error"]["message"]
    assert "target_frames" in message
    assert "greater than or equal to 30" in message
    assert "got 0" in message


def test_broker_down_fails_fast_without_stranding_a_queued_job(client):
    """Celery spends ~20s reconnecting by default, so an unreachable Redis
    turned submission into a long hang and then a 500 — while leaving a
    `queued` row behind that nothing would ever run."""
    import time

    from kombu.exceptions import OperationalError

    from app.database import get_session
    from app.models import Job

    session = get_session()
    try:
        before = session.query(Job).count()
    finally:
        session.close()

    with patch("app.api.routes.jobs.ffprobe", return_value=FAKE_PROBE), patch(
        "app.tasks.process_job.apply_async",
        side_effect=OperationalError("Error -2 connecting to redis:6379."),
    ):
        started = time.monotonic()
        resp = client.post(
            "/api/jobs/upload?filename=a.mp4&mode=adaptive",
            content=b"fake mp4 bytes" * 50,
            headers={"Content-Type": "application/octet-stream"},
        )
        elapsed = time.monotonic() - started

    assert resp.status_code == 503
    assert "redis" in resp.json()["error"]["message"].lower()
    assert elapsed < 5, f"should fail fast, took {elapsed:.1f}s"

    session = get_session()
    try:
        assert session.query(Job).count() == before, "no orphan job row left behind"
    finally:
        session.close()
