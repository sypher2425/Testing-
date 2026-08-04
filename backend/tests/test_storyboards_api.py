"""Storyboard API: listing, serving, download subset, and regeneration."""
import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.database import get_session
from app.main import app
from app.models import Job


@pytest.fixture()
def client():
    return TestClient(app)


def _completed_job_with_storyboards(*, with_sheets=True) -> str:
    """A completed job whose storyboard files exist on disk."""
    from app.storage import get_storage

    storage = get_storage()
    session = get_session()
    try:
        job = Job(
            original_filename="clip.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            status="completed",
            current_step="zipping",
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    if with_sheets:
        buf = io.BytesIO()
        Image.new("RGB", (120, 80), (20, 30, 40)).save(buf, "JPEG")
        storage.save_bytes(f"{job_id}/storyboards/adaptive_storyboard_01.jpg", buf.getvalue())
        storage.save_bytes(
            f"{job_id}/storyboard_manifest.json",
            json.dumps(
                {
                    "status": "success",
                    "types_built": ["adaptive"],
                    "layout": {"columns": 5, "sheet_width": 2400},
                    "storyboards": [
                        {
                            "type": "adaptive",
                            "file": "storyboards/adaptive_storyboard_01.jpg",
                            "sheet_index": 1,
                            "sheet_count": 1,
                            "columns": 5,
                            "rows": 1,
                            "frames": [
                                {
                                    "tileIndex": 1,
                                    "frameNumber": 0,
                                    "timestampSeconds": 0.0,
                                    "timestampLabel": "00:00.000",
                                    "sourceFrame": "frames/0000.000.jpg",
                                    "transcriptSegment": None,
                                }
                            ],
                        }
                    ],
                }
            ).encode(),
        )
    return job_id


# ------------------------------------------------------------------- listing


def test_list_storyboards_returns_the_manifest(client):
    job_id = _completed_job_with_storyboards()
    resp = client.get(f"/api/jobs/{job_id}/storyboards")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["storyboards"][0]["file"] == "storyboards/adaptive_storyboard_01.jpg"
    assert body["storyboards"][0]["frames"][0]["sourceFrame"] == "frames/0000.000.jpg"


def test_list_storyboards_is_an_empty_manifest_not_a_404(client):
    """The UI needs to render an explanatory empty state, not an error."""
    job_id = _completed_job_with_storyboards(with_sheets=False)
    resp = client.get(f"/api/jobs/{job_id}/storyboards")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_available", "storyboards": [], "types_built": []}


def test_list_storyboards_unknown_job_is_404(client):
    resp = client.get("/api/jobs/nope/storyboards")
    assert resp.status_code == 404


# ------------------------------------------------------------------- serving


def test_get_storyboard_serves_the_image(client):
    job_id = _completed_job_with_storyboards()
    resp = client.get(f"/api/jobs/{job_id}/storyboards/adaptive_storyboard_01.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(resp.content)).format == "JPEG"


def test_get_missing_storyboard_is_404(client):
    job_id = _completed_job_with_storyboards()
    resp = client.get(f"/api/jobs/{job_id}/storyboards/nope_01.jpg")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "attack",
    ["..%2F..%2Fmanifest.json", "..%2Fsource%2Fvideo.mp4", "%2Fetc%2Fpasswd"],
)
def test_storyboard_path_traversal_is_rejected(client, attack):
    job_id = _completed_job_with_storyboards()
    resp = client.get(f"/api/jobs/{job_id}/storyboards/{attack}")
    assert resp.status_code in (400, 404)
    assert b"root:" not in resp.content


# ------------------------------------------------------------------ download


def test_download_storyboards_subset(client):
    import zipfile

    job_id = _completed_job_with_storyboards()
    resp = client.get(f"/api/jobs/{job_id}/download?asset=storyboards")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "storyboards/adaptive_storyboard_01.jpg" in names
    assert "storyboard_manifest.json" in names
    # The subset must not drag in the whole dataset.
    assert not any(n.startswith("frames/") for n in names)


def test_download_rejects_unknown_asset(client):
    job_id = _completed_job_with_storyboards()
    assert client.get(f"/api/jobs/{job_id}/download?asset=nonsense").status_code == 422


def test_existing_download_assets_still_work(client):
    """Adding an enum value must not break the ones already in use."""
    job_id = _completed_job_with_storyboards()
    for asset in ("transcript", "frames"):
        assert client.get(f"/api/jobs/{job_id}/download?asset={asset}").status_code == 200


# ---------------------------------------------------------------- regenerate


def test_regenerate_queues_the_task(client):
    job_id = _completed_job_with_storyboards()
    with patch("app.tasks.regenerate_storyboards.delay") as mock_delay:
        resp = client.post(f"/api/jobs/{job_id}/storyboards/regenerate")
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    mock_delay.assert_called_once_with(job_id)


def test_regenerate_rejects_unfinished_jobs(client):
    from app.storage import get_storage

    session = get_session()
    try:
        job = Job(
            original_filename="busy.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            status="transcribing",
            current_step="transcribing",
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    resp = client.post(f"/api/jobs/{job_id}/storyboards/regenerate")
    assert resp.status_code == 400
    assert "not completed" in resp.json()["error"]["message"]


def test_regenerate_rejects_research_jobs(client):
    session = get_session()
    try:
        job = Job(
            job_type="research",
            original_filename="Research: x",
            stored_source_filename="",
            mode="research",
            status="completed",
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    resp = client.post(f"/api/jobs/{job_id}/storyboards/regenerate")
    assert resp.status_code == 400


def test_regenerate_task_rebuilds_without_touching_frames(tmp_path):
    """The whole point: sheets are rebuilt from frames already on disk, and
    the frames themselves are left exactly as they were."""
    from app.storage import get_storage
    from app.tasks import regenerate_storyboards

    storage = get_storage()
    session = get_session()
    try:
        job = Job(
            original_filename="clip.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            status="completed",
            options={},
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    # Lay down frames + the metadata a completed job would have.
    frames_meta = []
    for i in range(4):
        buf = io.BytesIO()
        Image.new("RGB", (180, 320), (30 + i * 30, 60, 120)).save(buf, "JPEG")
        storage.save_bytes(f"{job_id}/frames/{i:04d}.000.jpg", buf.getvalue())
        frames_meta.append(
            {
                "frame": i,
                "timestamp": float(i),
                "image": f"{i:04d}.000.jpg",
                "mode": "adaptive",
                "category": "adaptive",
            }
        )
    storage.save_bytes(
        f"{job_id}/metadata/frames.json", json.dumps(frames_meta).encode()
    )
    storage.save_bytes(f"{job_id}/source/video.mp4", b"fake video bytes")
    storage.save_bytes(
        f"{job_id}/transcript/transcript.json",
        json.dumps({"language": "en", "skipped": False, "segments": [
            {"start": 0.0, "end": 3.0, "text": "spoken line"}
        ]}).encode(),
    )
    storage.save_bytes(
        f"{job_id}/manifest.json",
        json.dumps(
            {
                "video": {"duration_seconds": 4.0, "width": 180, "height": 320},
                "frame_counts": {"adaptive": 4, "opening_dense": 0, "key_events": 0},
                "processing": {"started_at": "2026-01-01T00:00:00+00:00"},
            }
        ).encode(),
    )

    frames_before = {
        p.name: p.read_bytes()
        for p in storage.get(f"{job_id}/frames").iterdir()
        if p.is_file()
    }

    result = regenerate_storyboards(job_id)

    assert result["status"] == "success"
    assert result["sheets"] >= 1

    # Sheets exist...
    manifest = json.loads(storage.get(f"{job_id}/storyboard_manifest.json").read_bytes())
    assert manifest["status"] == "success"
    for sheet in manifest["storyboards"]:
        assert storage.exists(f"{job_id}/" + sheet["file"])

    # ...and the original frames are byte-for-byte untouched.
    frames_after = {
        p.name: p.read_bytes()
        for p in storage.get(f"{job_id}/frames").iterdir()
        if p.is_file()
    }
    assert frames_after == frames_before

    # The rebuild is recorded in the manifest.
    rebuilt = json.loads(storage.get(f"{job_id}/manifest.json").read_bytes())
    assert rebuilt["processing"]["last_rebuilt_at"] is not None
    assert rebuilt["storyboards"]["status"] == "success"
    # And the ZIP was refreshed so the download contains the new sheets.
    assert storage.exists(f"{job_id}/output.zip")


def test_regenerate_works_with_real_persisted_job_options(tmp_path):
    """The reported failure: Regenerate on a job whose stored options came
    from CreateJobOptions().model_dump() crashed with int(None) because the
    nullable storyboard fields persist as explicit None."""
    from app.schemas import CreateJobOptions
    from app.storage import get_storage
    from app.tasks import regenerate_storyboards

    storage = get_storage()
    persisted = CreateJobOptions().model_dump(mode="json")
    persisted["performance_overrides"] = {}
    persisted["events"] = []
    assert persisted["storyboard_tiles_per_sheet"] is None  # the shape that broke

    session = get_session()
    try:
        job = Job(
            original_filename="real.mp4",
            stored_source_filename="video.mp4",
            mode="adaptive",
            status="completed",
            options=persisted,
        )
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    frames_meta = []
    for i in range(4):
        buf = io.BytesIO()
        Image.new("RGB", (180, 320), (40 + i * 30, 70, 130)).save(buf, "JPEG")
        storage.save_bytes(f"{job_id}/frames/{i:04d}.000.jpg", buf.getvalue())
        frames_meta.append(
            {
                "frame": i,
                "timestamp": float(i),
                "image": f"{i:04d}.000.jpg",
                "mode": "adaptive",
                "category": "adaptive",
            }
        )
    storage.save_bytes(f"{job_id}/metadata/frames.json", json.dumps(frames_meta).encode())
    storage.save_bytes(f"{job_id}/source/video.mp4", b"fake")
    storage.save_bytes(
        f"{job_id}/manifest.json",
        json.dumps(
            {
                "video": {"duration_seconds": 4.0, "width": 180, "height": 320},
                "frame_counts": {"adaptive": 4, "opening_dense": 0, "key_events": 0},
                "processing": {"started_at": "2026-01-01T00:00:00+00:00"},
            }
        ).encode(),
    )

    result = regenerate_storyboards(job_id)

    assert result["status"] == "success", result
    assert result["sheets"] >= 1
    manifest = json.loads(storage.get(f"{job_id}/storyboard_manifest.json").read_bytes())
    assert manifest["status"] == "success"
    # Captions must not have silently flipped off via bool(None).
    assert manifest["layout"]["captions"] is True


def test_regenerate_task_on_missing_job_is_a_no_op():
    from app.tasks import regenerate_storyboards

    assert regenerate_storyboards("does-not-exist")["status"] == "not_found"
