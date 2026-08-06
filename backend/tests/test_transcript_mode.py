"""Transcript mode: any platform link in, a transcript out.

The behaviours worth pinning down are the ones that make it cheap and
honest: captions before audio, audio-only when Whisper is needed, one
transcript shape either way, and provenance recorded rather than inferred.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import steps_for_job_type
from app.pipeline.errors import PipelineFailedError
from app.pipeline.steps.transcript import (
    TranscriptManifestStep,
    TranscriptModelStep,
    TranscriptSourceStep,
    TranscriptTranscribeStep,
    _caption_langs,
)
from app.utils.captions import subtitles_to_segments
from app.utils.ytdlp import VideoMetadata
from tests.test_pipeline_steps import make_ctx

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.500 --> 00:00:03.000
Hello and welcome back

00:00:03.000 --> 00:00:06.250
Hello and welcome back
to the channel

00:00:06.250 --> 00:00:09.000
[music]

00:00:09.000 --> 00:00:12.000
<c.colorE5E5E5>Today we are building</c> something
"""

SRT = """1
00:00:01,000 --> 00:00:02,500
First line

2
00:00:02,500 --> 00:00:04,000
Second line
"""


def _meta(**overrides) -> VideoMetadata:
    base = dict(
        source_url="https://www.tiktok.com/@x/video/123",
        platform="tiktok",
        title="A Clip",
        description=None,
        uploader="x",
        uploader_id="x",
        upload_date="2026-08-01",
        view_count=None,
        like_count=None,
        comment_count=None,
        share_count=None,
        hashtags=[],
        duration=12.0,
        ext="mp4",
        filesize_approx=None,
        raw={"id": "123", "subtitles": {"en": [{}]}},
    )
    base.update(overrides)
    return VideoMetadata(**base)


# --------------------------------------------------------------- parsing


def test_subtitles_to_segments_keeps_timing_and_cleans_text():
    segments = subtitles_to_segments(VTT)
    assert segments[0] == {"start": 0.5, "end": 3.0, "text": "Hello and welcome back"}
    # The rolling duplicate cue is dropped, its unique continuation kept.
    assert segments[1]["text"] == "to the channel"
    # A music-only cue carries no speech.
    assert all("music" not in s["text"] for s in segments)
    # Inline styling tags are stripped, the text inside them is not.
    assert segments[-1]["text"] == "Today we are building something"
    assert segments[-1]["end"] == 12.0


def test_subtitles_to_segments_parses_srt_comma_timestamps():
    assert subtitles_to_segments(SRT) == [
        {"start": 1.0, "end": 2.5, "text": "First line"},
        {"start": 2.5, "end": 4.0, "text": "Second line"},
    ]


def test_subtitles_to_segments_handles_short_vtt_timestamps():
    """VTT allows mm:ss.mmm with no hour field."""
    segments = subtitles_to_segments("WEBVTT\n\n01:02.500 --> 01:04.000\nlate line\n")
    assert segments == [{"start": 62.5, "end": 64.0, "text": "late line"}]


def test_subtitles_to_segments_fraction_digits_are_not_misread():
    """'.5' is 500ms, not 5ms — getting this wrong shifts every timestamp."""
    segments = subtitles_to_segments("WEBVTT\n\n00:00.5 --> 00:01.25\nx\n")
    assert segments == [{"start": 0.5, "end": 1.25, "text": "x"}]


def test_subtitles_to_segments_on_garbage_returns_nothing_rather_than_guessing():
    assert subtitles_to_segments("not a subtitle file at all") == []


def test_caption_language_selector():
    assert _caption_langs(None) == "en.*,en"
    assert _caption_langs("es") == "es.*,es"


# ---------------------------------------------------------- captions path


def _write_caption_file(directory: Path, name: str, body: str):
    def fake_download(url, dest_dir, video_id, *, manual, log, sub_langs=None):
        path = Path(dest_dir) / name
        path.write_text(body, encoding="utf-8")
        return path

    return fake_download


def test_captions_path_produces_a_transcript_without_downloading_audio(tmp_path):
    ctx, logs, _ = make_ctx(
        tmp_path, options={"transcript": {"url": "https://www.tiktok.com/@x/video/123"}}
    )
    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=_meta()), patch(
        "app.pipeline.steps.transcript.download_captions",
        side_effect=_write_caption_file(tmp_path, "123.en.vtt", VTT),
    ), patch("app.pipeline.steps.transcript.download_audio") as mock_audio:
        TranscriptSourceStep().run(ctx)

    mock_audio.assert_not_called()
    captured = ctx.shared["caption_transcript"]
    assert captured["transcript_source"] == "platform_captions"
    assert captured["caption_track"] == "manual"
    assert captured["language"] == "en"
    assert len(captured["segments"]) == 3


def test_model_load_is_skipped_when_captions_already_won(tmp_path):
    """Loading a ~460MB model to transcribe nothing is the waste this avoids."""
    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["caption_transcript"] = {"segments": [], "language": "en"}
    with patch("app.pipeline.steps.transcript.LoadWhisperModelStep.run") as mock_parent:
        TranscriptModelStep().run(ctx)
    mock_parent.assert_not_called()


def test_model_loads_normally_when_whisper_is_needed(tmp_path):
    ctx, _, _ = make_ctx(tmp_path)
    ctx.shared["video"] = {"has_audio": True}
    with patch("app.pipeline.steps.load_model.load_whisper_model", return_value=object()):
        TranscriptModelStep().run(ctx)
    assert ctx.shared["whisper_model"] is not None


def test_caption_transcript_is_written_in_all_three_formats(tmp_path):
    ctx, _, shared_state = make_ctx(tmp_path)
    ctx.shared["caption_transcript"] = {
        "language": "en",
        "duration": 12.0,
        "skipped": False,
        "skipped_reason": None,
        "transcript_source": "platform_captions",
        "caption_track": "automatic",
        "segments": [{"start": 0.5, "end": 3.0, "text": "Hello and welcome back"}],
    }
    ctx.shared["caption_plain_text"] = "Hello and welcome back\n"

    TranscriptTranscribeStep().run(ctx)

    written = json.loads(ctx.storage.get(ctx.job_relative("transcript", "transcript.json")).read_bytes())
    assert written["transcript_source"] == "platform_captions"
    assert written["segments"][0]["text"] == "Hello and welcome back"
    srt = ctx.storage.get(ctx.job_relative("transcript", "subtitles.srt")).read_text()
    assert "00:00:00,500 --> 00:00:03,000" in srt
    txt = ctx.storage.get(ctx.job_relative("transcript", "transcript.txt")).read_text()
    assert txt == "Hello and welcome back\n"
    assert shared_state["language"] == "en"


def test_automatic_captions_are_used_when_there_are_no_manual_ones(tmp_path):
    ctx, logs, _ = make_ctx(tmp_path, options={"transcript": {"url": "https://x.test/v"}})
    meta = _meta(raw={"id": "123", "automatic_captions": {"en": [{}]}})
    calls = []

    def fake_download(url, dest_dir, video_id, *, manual, log, sub_langs=None):
        calls.append(manual)
        path = Path(dest_dir) / "123.en.vtt"
        path.write_text(VTT, encoding="utf-8")
        return path

    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=meta), patch(
        "app.pipeline.steps.transcript.download_captions", side_effect=fake_download
    ):
        TranscriptSourceStep().run(ctx)

    assert calls == [False], "must not request manual subs the platform doesn't advertise"
    assert ctx.shared["caption_transcript"]["caption_track"] == "automatic"


# ----------------------------------------------------------- whisper path


def test_falls_back_to_audio_only_download_when_there_are_no_captions(tmp_path):
    ctx, logs, shared_state = make_ctx(
        tmp_path, options={"transcript": {"url": "https://x.test/v"}}
    )
    audio = tmp_path / "job1" / "source" / "audio.m4a"

    def fake_audio(url, dest_dir, *, log):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"fake audio")
        return audio

    with patch(
        "app.pipeline.steps.transcript.extract_metadata", return_value=_meta(raw={"id": "123"})
    ), patch(
        "app.pipeline.steps.transcript.download_audio", side_effect=fake_audio
    ) as mock_audio, patch(
        "app.pipeline.steps.transcript.download_captions"
    ) as mock_captions:
        TranscriptSourceStep().run(ctx)

    # No advertised caption track means no pointless caption request either.
    mock_captions.assert_not_called()
    mock_audio.assert_called_once()
    assert ctx.shared["stored_source_filename"] == "audio.m4a"
    assert shared_state["file_size_bytes"] == len(b"fake audio")


def test_whisper_transcript_records_its_provenance(tmp_path):
    """Whichever path ran, transcript.json must say which one it was."""
    ctx, _, _ = make_ctx(tmp_path)

    def fake_parent_run(self, c):
        transcript = {
            "language": "en",
            "duration": 12.0,
            "skipped": False,
            "skipped_reason": None,
            "segments": [{"start": 0.0, "end": 2.0, "text": "spoken"}],
        }
        self._write_outputs(c, transcript)
        c.shared["transcript"] = transcript

    with patch("app.pipeline.steps.transcribe.TranscribeStep.run", fake_parent_run):
        TranscriptTranscribeStep().run(ctx)

    written = json.loads(ctx.storage.get(ctx.job_relative("transcript", "transcript.json")).read_bytes())
    assert written["transcript_source"] == "whisper"
    assert written["caption_track"] is None
    # The parent's .txt/.srt output is still intact.
    assert ctx.storage.get(ctx.job_relative("transcript", "transcript.txt")).read_text() == "spoken"


# ------------------------------------------------------------ preferences


def test_captions_only_fails_loudly_instead_of_downloading_audio(tmp_path):
    ctx, _, _ = make_ctx(
        tmp_path,
        options={"transcript": {"url": "https://x.test/v", "source_preference": "captions_only"}},
    )
    with patch(
        "app.pipeline.steps.transcript.extract_metadata", return_value=_meta(raw={"id": "123"})
    ), patch("app.pipeline.steps.transcript.download_audio") as mock_audio:
        with pytest.raises(PipelineFailedError) as exc_info:
            TranscriptSourceStep().run(ctx)

    mock_audio.assert_not_called()
    assert exc_info.value.code == "captions_unavailable"


def test_whisper_only_ignores_available_captions(tmp_path):
    ctx, _, _ = make_ctx(
        tmp_path,
        options={"transcript": {"url": "https://x.test/v", "source_preference": "whisper_only"}},
    )

    def fake_audio(url, dest_dir, *, log):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / "audio.webm"
        path.write_bytes(b"audio")
        return path

    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=_meta()), patch(
        "app.pipeline.steps.transcript.download_captions"
    ) as mock_captions, patch(
        "app.pipeline.steps.transcript.download_audio", side_effect=fake_audio
    ):
        TranscriptSourceStep().run(ctx)

    mock_captions.assert_not_called()
    assert "caption_transcript" not in ctx.shared


def test_empty_captions_fall_through_to_whisper_rather_than_shipping_nothing(tmp_path):
    ctx, logs, _ = make_ctx(tmp_path, options={"transcript": {"url": "https://x.test/v"}})

    def empty_captions(url, dest_dir, video_id, *, manual, log, sub_langs=None):
        path = Path(dest_dir) / "123.en.vtt"
        path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n[music]\n", encoding="utf-8")
        return path

    def fake_audio(url, dest_dir, *, log):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / "audio.m4a"
        path.write_bytes(b"audio")
        return path

    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=_meta()), patch(
        "app.pipeline.steps.transcript.download_captions", side_effect=empty_captions
    ), patch("app.pipeline.steps.transcript.download_audio", side_effect=fake_audio) as mock_audio:
        TranscriptSourceStep().run(ctx)

    mock_audio.assert_called_once()
    assert any("empty after cleaning" in m for _, m in logs)


def test_caption_download_failure_is_a_fallback_not_a_job_failure(tmp_path):
    ctx, logs, _ = make_ctx(tmp_path, options={"transcript": {"url": "https://x.test/v"}})

    def fake_audio(url, dest_dir, *, log):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        path = Path(dest_dir) / "audio.m4a"
        path.write_bytes(b"audio")
        return path

    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=_meta()), patch(
        "app.pipeline.steps.transcript.download_captions", side_effect=RuntimeError("boom")
    ), patch("app.pipeline.steps.transcript.download_audio", side_effect=fake_audio) as mock_audio:
        TranscriptSourceStep().run(ctx)  # must not raise

    mock_audio.assert_called_once()


def test_unresolvable_link_fails_the_job_with_the_ytdlp_code(tmp_path):
    from app.utils.ytdlp import YtDlpError

    ctx, _, _ = make_ctx(tmp_path, options={"transcript": {"url": "https://x.test/v"}})
    with patch(
        "app.pipeline.steps.transcript.extract_metadata",
        side_effect=YtDlpError("video_unavailable", "Private video", stderr="ERROR: Private video"),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            TranscriptSourceStep().run(ctx)
    assert exc_info.value.code == "video_unavailable"


# -------------------------------------------------------------- manifest


def test_manifest_reports_coverage_and_provenance(tmp_path):
    ctx, _, _ = make_ctx(
        tmp_path, options={"transcript": {"url": "https://x.test/v", "source_preference": "captions_first"}}
    )
    ctx.shared["transcript_meta"] = {"source_url": "https://x.test/v", "platform": "tiktok", "duration_seconds": 12.0}
    ctx.shared["transcript"] = {
        "language": "en",
        "duration": 12.0,
        "transcript_source": "platform_captions",
        "caption_track": "manual",
        "segments": [
            {"start": 0.5, "end": 3.0, "text": "two words here"},
            {"start": 3.0, "end": 7.0, "text": "and three more"},
        ],
    }

    TranscriptManifestStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    assert manifest["job_type"] == "transcript"
    assert manifest["transcript"]["segment_count"] == 2
    assert manifest["transcript"]["word_count"] == 6
    assert manifest["transcript"]["transcript_source"] == "platform_captions"
    # Coverage is the transcript's own span, not the video's duration.
    assert manifest["transcript"]["first_segment_start_seconds"] == 0.5
    assert manifest["transcript"]["last_segment_end_seconds"] == 7.0
    assert manifest["transcript"]["duration_seconds"] == 12.0


def test_manifest_does_not_invent_coverage_for_an_empty_transcript(tmp_path):
    ctx, _, _ = make_ctx(tmp_path, options={"transcript": {"url": "https://x.test/v"}})
    ctx.shared["transcript"] = {"language": None, "transcript_source": "whisper", "segments": []}

    TranscriptManifestStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("manifest.json")).read_bytes())
    assert manifest["transcript"]["segment_count"] == 0
    assert manifest["transcript"]["first_segment_start_seconds"] is None
    assert manifest["transcript"]["last_segment_end_seconds"] is None


# --------------------------------------------------------------- wiring


def test_transcript_pipeline_reuses_existing_step_names():
    """The whole point of the step-name reuse: no new job states, so no
    frontend status map can go stale."""
    from app.models import PIPELINE_STEPS
    from app.pipeline.runner import _build_pipeline

    names = [s.name for s in _build_pipeline("transcript")]
    assert names == steps_for_job_type("transcript")[1:]
    assert set(names) <= set(PIPELINE_STEPS)


# ------------------------------------------------------------------ route


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _completed_transcript_job(client) -> str:
    from app.database import get_session
    from app.models import Job

    with patch("app.tasks.process_job.delay"):
        job_id = client.post(
            "/api/jobs/transcript", json={"url": "https://www.tiktok.com/@x/video/123"}
        ).json()["job_id"]

    session = get_session()
    try:
        session.get(Job, job_id).status = "completed"
        session.commit()
    finally:
        session.close()
    return job_id


def test_create_transcript_job_enqueues_and_records_its_options(client):
    with patch("app.tasks.process_job.delay") as mock_delay:
        resp = client.post(
            "/api/jobs/transcript",
            json={"url": "https://www.tiktok.com/@x/video/123", "language": "es"},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(job_id)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["job_type"] == "transcript"
    assert body["mode"] == "transcript"
    assert body["source_url"] == "https://www.tiktok.com/@x/video/123"
    assert body["options"]["transcript"]["source_preference"] == "captions_first"
    assert body["options"]["transcript"]["language"] == "es"


def test_create_transcript_job_rejects_bad_input(client):
    assert client.post("/api/jobs/transcript", json={"url": ""}).status_code == 422
    assert (
        client.post(
            "/api/jobs/transcript", json={"url": "https://x.test/v", "source_preference": "magic"}
        ).status_code
        == 422
    )
    # Same SSRF guard as the video-URL route.
    assert client.post("/api/jobs/transcript", json={"url": "http://localhost/v"}).status_code == 400
    assert client.post("/api/jobs/transcript", json={"url": "file:///etc/passwd"}).status_code == 400


def test_transcript_job_download_allows_only_zip_and_transcript(client):
    job_id = _completed_transcript_job(client)
    for asset in ("frames", "storyboards"):
        resp = client.get(f"/api/jobs/{job_id}/download?asset={asset}")
        assert resp.status_code == 400
        assert "only support" in resp.json()["error"]["message"]


def test_transcript_job_has_no_storyboards_to_regenerate(client):
    job_id = _completed_transcript_job(client)
    resp = client.post(f"/api/jobs/{job_id}/storyboards/regenerate")
    assert resp.status_code == 400
    assert "no storyboards" in resp.json()["error"]["message"].lower()


# --------------------------------------------------------- end-to-end run


def test_full_transcript_pipeline_end_to_end_via_captions(db_session, storage):
    """The real runner, the real ZIP step, a real DB row — only the network
    boundary (yt-dlp) is faked. Whisper never runs, so this needs no model."""
    import uuid
    import zipfile

    from app.database import get_session
    from app.models import Job
    from app.pipeline.runner import run_pipeline

    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        job_type="transcript",
        original_filename="Transcript: https://x.test/v",
        stored_source_filename="",
        status="queued",
        mode="transcript",
        source_url="https://x.test/v",
        options={"transcript": {"url": "https://x.test/v", "source_preference": "captions_first"}},
        step_progress={},
    )
    db_session.add(job)
    db_session.commit()

    def fake_captions(url, dest_dir, video_id, *, manual, log, sub_langs=None):
        path = Path(dest_dir) / "123.en.vtt"
        path.write_text(VTT, encoding="utf-8")
        return path

    with patch("app.pipeline.steps.transcript.extract_metadata", return_value=_meta()), patch(
        "app.pipeline.steps.transcript.download_captions", side_effect=fake_captions
    ), patch("app.pipeline.steps.transcript.download_audio") as mock_audio:
        run_pipeline(job_id)

    mock_audio.assert_not_called()

    session = get_session()
    try:
        refreshed = session.get(Job, job_id)
        assert refreshed.status == "completed", refreshed.error_message
        assert refreshed.language == "en"
        # The placeholder title is replaced by the real one.
        assert refreshed.original_filename == "A Clip"
    finally:
        session.close()

    with zipfile.ZipFile(storage.get(f"{job_id}/output.zip")) as zf:
        names = set(zf.namelist())
    assert {"manifest.json", "transcript/transcript.json", "transcript/transcript.txt",
            "transcript/subtitles.srt"} <= names
    # No video, no frames, no storyboards — that is the whole point.
    assert not any(n.startswith(("frames/", "storyboards/", "source/")) for n in names)

    manifest = json.loads(storage.get(f"{job_id}/manifest.json").read_bytes())
    assert manifest["transcript"]["transcript_source"] == "platform_captions"
    assert manifest["transcript"]["segment_count"] == 3


# ------------------------------------------------- transcript from a file


def _probe(duration=4.0, has_audio=True):
    from app.utils.ffmpeg import ProbeResult

    return ProbeResult(
        duration_seconds=duration, width=180, height=320, fps=30.0,
        codec="h264", has_audio=has_audio, raw={},
    )


def test_uploaded_file_skips_ytdlp_entirely(tmp_path):
    """The whole point of this path: it works when the platform doesn't."""
    ctx, logs, shared_state = make_ctx(tmp_path, options={"transcript": {"url": None}})
    ctx.storage.save_bytes(ctx.job_relative("source", "source.mp4"), b"fake media")
    ctx.shared["stored_source_filename"] = "source.mp4"
    ctx.shared["original_filename"] = "my clip.mp4"

    with patch("app.pipeline.steps.transcript.ffprobe", return_value=_probe()), patch(
        "app.pipeline.steps.transcript.extract_metadata"
    ) as mock_meta, patch("app.pipeline.steps.transcript.download_captions") as mock_caps, patch(
        "app.pipeline.steps.transcript.download_audio"
    ) as mock_audio:
        TranscriptSourceStep().run(ctx)

    for mock in (mock_meta, mock_caps, mock_audio):
        mock.assert_not_called()
    assert ctx.shared["source_relative_path"].endswith("source.mp4")
    assert ctx.shared["video"] == {"duration_seconds": 4.0, "has_audio": True}
    assert ctx.shared["transcript_meta"]["platform"] == "upload"
    assert ctx.shared["transcript_meta"]["title"] == "my clip.mp4"
    assert "caption_transcript" not in ctx.shared


def test_uploaded_file_with_no_audio_fails_instead_of_returning_an_empty_transcript(tmp_path):
    """A full dataset job survives a silent video because frames still have
    value. Here there is nothing else to produce, so success would be a lie."""
    ctx, _, _ = make_ctx(tmp_path, options={"transcript": {"url": None}})
    ctx.storage.save_bytes(ctx.job_relative("source", "source.mp4"), b"fake")
    ctx.shared["stored_source_filename"] = "source.mp4"

    with patch("app.pipeline.steps.transcript.ffprobe", return_value=_probe(has_audio=False)):
        with pytest.raises(PipelineFailedError) as exc_info:
            TranscriptSourceStep().run(ctx)

    assert exc_info.value.code == "no_audio_track"
    assert "nothing to transcribe" in exc_info.value.message


def test_neither_a_url_nor_a_file_is_a_clear_error(tmp_path):
    ctx, _, _ = make_ctx(tmp_path, options={"transcript": {"url": None}})
    with pytest.raises(PipelineFailedError) as exc_info:
        TranscriptSourceStep().run(ctx)
    assert exc_info.value.code == "invalid_request"


def test_unreadable_upload_fails_with_the_probe_detail(tmp_path):
    from app.utils.ffmpeg import FFmpegError

    ctx, _, _ = make_ctx(tmp_path, options={"transcript": {"url": None}})
    ctx.storage.save_bytes(ctx.job_relative("source", "source.mp4"), b"not media")
    ctx.shared["stored_source_filename"] = "source.mp4"

    with patch(
        "app.pipeline.steps.transcript.ffprobe",
        side_effect=FFmpegError(
            "ffprobe could not read the file", cmd=["ffprobe"], returncode=1, stderr="invalid data"
        ),
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            TranscriptSourceStep().run(ctx)
    assert exc_info.value.code == "probe_failed"


def test_upload_route_accepts_audio_only_files(client):
    import io

    with patch("app.api.routes.jobs.ffprobe", return_value=_probe()), patch(
        "app.tasks.process_job.delay"
    ) as mock_delay:
        resp = client.post(
            "/api/jobs/transcript/upload?filename=voice-memo.mp3",
            content=b"fake audio bytes" * 100,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    mock_delay.assert_called_once_with(job_id)

    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["job_type"] == "transcript"
    assert body["original_filename"] == "voice-memo.mp3"
    # No captions exist for a local file, so the preference is not a choice.
    assert body["options"]["transcript"]["source_preference"] == "whisper_only"
    assert body["options"]["transcript"]["url"] is None


def test_upload_route_rejects_a_non_media_extension(client):
    resp = client.post(
        "/api/jobs/transcript/upload?filename=notes.pdf",
        content=b"%PDF-1.4",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 422
    assert "mp3" in resp.json()["error"]["message"]


def test_upload_route_rejects_an_empty_body_without_leaving_a_job_behind(client):
    from app.database import get_session
    from app.models import Job

    before = get_session()
    try:
        count_before = before.query(Job).count()
    finally:
        before.close()

    resp = client.post(
        "/api/jobs/transcript/upload?filename=empty.mp4",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 400

    after = get_session()
    try:
        assert after.query(Job).count() == count_before
    finally:
        after.close()


def test_upload_route_rejects_a_file_ffprobe_cannot_read(client, storage):
    from app.utils.ffmpeg import FFmpegError

    with patch(
        "app.api.routes.jobs.ffprobe",
        side_effect=FFmpegError(
            "not media", cmd=["ffprobe"], returncode=1, stderr="invalid data"
        ),
    ):
        resp = client.post(
            "/api/jobs/transcript/upload?filename=fake.mp4",
            content=b"definitely not a video",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert resp.status_code == 422
    assert "media validation" in resp.json()["error"]["message"]


def test_uploaded_audio_file_runs_the_whole_pipeline(db_session, storage):
    """A real .m4a through the real runner: real ffprobe, real ffmpeg WAV
    extraction, real manifest and ZIP. Only the model weights are stubbed —
    they need a network fetch, and they are not what this path changed.

    The audio container matters: every other caller feeds these code paths an
    .mp4, so an .m4a is genuinely new input for both ffprobe and
    extract_audio_wav."""
    import shutil
    import subprocess
    import uuid
    import zipfile

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    class _StubSegment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class _StubInfo:
        language = "en"

    class _StubModel:
        def transcribe(self, audio_path, **kwargs):
            # Proves the step got a readable WAV out of the .m4a.
            assert Path(audio_path).stat().st_size > 0
            return iter([_StubSegment(0.0, 3.0, " a steady tone")]), _StubInfo()

    job_id = str(uuid.uuid4())
    dest = storage.get(f"{job_id}/source/source.m4a")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:a", "aac", str(dest)],
        check=True, capture_output=True,
    )

    from app.database import get_session
    from app.models import Job
    from app.pipeline.runner import run_pipeline

    job = Job(
        id=job_id,
        job_type="transcript",
        original_filename="voice-memo.m4a",
        stored_source_filename="source.m4a",
        status="queued",
        mode="transcript",
        options={"transcript": {"url": None, "source_preference": "whisper_only", "language": None}},
        step_progress={},
    )
    db_session.add(job)
    db_session.commit()

    with patch("app.pipeline.steps.load_model.load_whisper_model", return_value=_StubModel()):
        run_pipeline(job_id)

    session = get_session()
    try:
        refreshed = session.get(Job, job_id)
        assert refreshed.status == "completed", refreshed.error_message
        assert refreshed.duration_seconds and refreshed.duration_seconds > 2.5
        assert refreshed.language == "en"
    finally:
        session.close()

    transcript = json.loads(storage.get(f"{job_id}/transcript/transcript.json").read_bytes())
    assert transcript["segments"][0]["text"] == "a steady tone"
    assert transcript["transcript_source"] == "whisper"
    assert transcript["caption_track"] is None

    manifest = json.loads(storage.get(f"{job_id}/manifest.json").read_bytes())
    assert manifest["source"]["platform"] == "upload"
    assert manifest["source"]["source_url"] is None

    with zipfile.ZipFile(storage.get(f"{job_id}/output.zip")) as zf:
        names = set(zf.namelist())
    assert "transcript/transcript.json" in names
    assert not any(n.startswith(("frames/", "storyboards/")) for n in names)


def test_uploaded_file_records_what_it_measured_rather_than_leaving_nulls(tmp_path):
    ctx, _, shared_state = make_ctx(tmp_path, options={"transcript": {"url": None}})
    ctx.storage.save_bytes(ctx.job_relative("source", "source.mp3"), b"fake")
    ctx.shared["stored_source_filename"] = "source.mp3"

    with patch("app.pipeline.steps.transcript.ffprobe", return_value=_probe(duration=5.04)):
        TranscriptSourceStep().run(ctx)

    assert shared_state["duration_seconds"] == 5.04
    assert shared_state["has_audio"] is True
    assert shared_state["codec"] == "h264"
