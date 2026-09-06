"""Runs the full pipeline (probe -> transcribe -> extract frames -> metadata ->
zip) against a real, generated sample video using the real ffmpeg/ffprobe and
a real (tiny) faster-whisper model. Skipped automatically if ffmpeg isn't on
PATH or the whisper model can't be loaded (e.g. no network access to fetch
model weights), since those are environment constraints rather than bugs."""
import json
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest

from app.database import get_session
from app.models import Job
from app.pipeline.runner import run_pipeline
from app.storage.local import LocalStorageBackend

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample"
SAMPLE_VIDEO = SAMPLE_DIR / "sample.mp4"

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


@pytest.fixture(scope="module", autouse=True)
def _ensure_sample_video():
    if not SAMPLE_VIDEO.exists():
        script = Path(__file__).resolve().parent.parent / "scripts" / "generate_sample.py"
        subprocess.run(["python", str(script)], check=True)
    if not SAMPLE_VIDEO.exists():
        pytest.skip("could not generate sample video")


@pytest.fixture()
def whisper_available():
    try:
        from faster_whisper import WhisperModel

        WhisperModel("tiny", device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"faster-whisper model unavailable in this environment: {exc}")


def test_full_pipeline_on_sample_video(db_session, storage, whisper_available):
    job_id = str(uuid.uuid4())
    dest = storage.get(f"{job_id}/source/video.mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SAMPLE_VIDEO, dest)

    job = Job(
        id=job_id,
        original_filename="sample.mp4",
        stored_source_filename="video.mp4",
        status="queued",
        mode="adaptive",
        options={"mode": "adaptive", "target_frames": 30, "frame_format": "jpeg", "frame_max_dim": 320},
        file_size_bytes=dest.stat().st_size,
        step_progress={},
    )
    db_session.add(job)
    db_session.commit()

    run_pipeline(job_id)

    session = get_session()
    try:
        refreshed = session.get(Job, job_id)
        assert refreshed.status == "completed", refreshed.error_message
        assert refreshed.frame_count and refreshed.frame_count > 0
        assert refreshed.duration_seconds and refreshed.duration_seconds > 0
    finally:
        session.close()

    manifest_path = storage.get(f"{job_id}/manifest.json")
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["job_id"] == job_id
    assert manifest["frame_count"] > 0
    assert len(manifest["files"]) >= manifest["frame_count"]

    zip_path = storage.get(f"{job_id}/output.zip")
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    assert any(n.startswith("frames/") for n in names)
    assert "transcript/transcript.json" in names
