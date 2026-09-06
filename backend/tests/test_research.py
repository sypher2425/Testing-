import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.pipeline.errors import PipelineFailedError
from app.pipeline.steps.research import (
    ResearchCaptionsStep,
    ResearchManifestStep,
    ResearchSearchStep,
    apply_filters,
)
from app.utils.ytdlp import VideoMetadata, YtDlpError, search_videos
from tests.test_pipeline_steps import make_ctx

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def _video(**overrides):
    base = {
        "id": "vid1",
        "title": "A Video",
        "channel": "Chan",
        "views": 10_000,
        "likes": 100,
        "upload_date": "2026-07-10",
        "duration": 300,
        "url": "https://www.youtube.com/watch?v=vid1",
        "has_manual_en": False,
        "has_auto_en": True,
    }
    base.update(overrides)
    return base


# ---------- filters ----------


def test_apply_filters_min_views_excludes_low_and_unknown():
    videos = [_video(views=100), _video(id="v2", views=50_000), _video(id="v3", views=None)]
    kept = apply_filters(videos, min_views=1000, uploaded_within_days=None, max_duration_seconds=None, now=NOW)
    assert [v["id"] for v in kept] == ["v2"]


def test_apply_filters_uploaded_within_days():
    videos = [
        _video(id="fresh", upload_date="2026-07-15"),
        _video(id="old", upload_date="2026-01-01"),
        _video(id="undated", upload_date=None),
    ]
    kept = apply_filters(videos, min_views=None, uploaded_within_days=30, max_duration_seconds=None, now=NOW)
    assert [v["id"] for v in kept] == ["fresh"]


def test_apply_filters_max_duration_keeps_unknown_duration():
    videos = [_video(id="short", duration=60), _video(id="long", duration=4000), _video(id="unknown", duration=None)]
    kept = apply_filters(videos, min_views=None, uploaded_within_days=None, max_duration_seconds=600, now=NOW)
    assert [v["id"] for v in kept] == ["short", "unknown"]


# ---------- search prefix ----------


def _completed(stdout: dict):
    return subprocess.CompletedProcess(["yt-dlp"], 0, stdout=json.dumps(stdout).encode(), stderr=b"")


def test_search_videos_uses_ytsearch_for_top_and_ytsearchdate_for_newest():
    captured = []

    def fake_retry(args, timeout, log):
        captured.append(args)
        return _completed({"entries": []})

    with patch("app.utils.ytdlp._run_with_extractor_retry", side_effect=fake_retry):
        search_videos("content strategy", 30, "top", log=lambda l, m: None)
        search_videos("content strategy", 30, "newest", log=lambda l, m: None)

    assert any(a == "ytsearch30:content strategy" for a in captured[0])
    assert any(a == "ytsearchdate30:content strategy" for a in captured[1])
    assert "--flat-playlist" in captured[0]


# ---------- search step ----------


def _meta(video_id: str, *, views: int, upload_date: str = "2026-07-10", subs=None, autos=None) -> VideoMetadata:
    return VideoMetadata(
        source_url=f"https://www.youtube.com/watch?v={video_id}",
        platform="youtube",
        title=f"Title {video_id}",
        description="",
        uploader="Chan",
        uploader_id="chan",
        upload_date=upload_date,
        view_count=views,
        like_count=10,
        comment_count=None,
        share_count=None,
        hashtags=[],
        duration=120.0,
        ext="mp4",
        filesize_approx=None,
        raw={"id": video_id, "subtitles": subs or {}, "automatic_captions": autos or {}},
    )


def test_search_step_sorts_top_by_views_and_retains_count(tmp_path):
    ctx, logs, _ = make_ctx(
        tmp_path,
        options={"research": {"query": "roblox", "result_count": 2, "sort_mode": "top"}},
    )
    entries = [{"id": f"v{i}", "title": f"t{i}", "url": f"https://youtu.be/v{i}"} for i in range(3)]
    metas = {
        "https://youtu.be/v0": _meta("v0", views=100),
        "https://youtu.be/v1": _meta("v1", views=99_999, autos={"en": []}),
        "https://youtu.be/v2": _meta("v2", views=5_000),
    }

    with patch("app.pipeline.steps.research.search_videos", return_value=entries), patch(
        "app.pipeline.steps.research.extract_metadata",
        side_effect=lambda url, **kw: metas[url],
    ):
        ResearchSearchStep().run(ctx)

    retained = ctx.shared["research_videos"]
    assert [v["id"] for v in retained] == ["v1", "v2"]  # views desc, top 2
    assert retained[0]["has_auto_en"] is True


def test_search_step_survives_per_candidate_failures(tmp_path):
    ctx, logs, _ = make_ctx(
        tmp_path,
        options={"research": {"query": "roblox", "result_count": 5, "sort_mode": "top"}},
    )
    entries = [{"id": "ok", "url": "https://youtu.be/ok"}, {"id": "bad", "url": "https://youtu.be/bad"}]

    def meta_or_fail(url, **kw):
        if "bad" in url:
            raise YtDlpError("video_unavailable", "Private video")
        return _meta("ok", views=10)

    with patch("app.pipeline.steps.research.search_videos", return_value=entries), patch(
        "app.pipeline.steps.research.extract_metadata", side_effect=meta_or_fail
    ):
        ResearchSearchStep().run(ctx)

    assert [v["id"] for v in ctx.shared["research_videos"]] == ["ok"]
    assert any("Skipping" in m for _, m in logs)


def test_search_step_fails_cleanly_when_nothing_matches(tmp_path):
    ctx, _, _ = make_ctx(
        tmp_path,
        options={"research": {"query": "roblox", "result_count": 5, "sort_mode": "top", "min_views": 10_000}},
    )
    entries = [{"id": "v0", "url": "https://youtu.be/v0"}]

    with patch("app.pipeline.steps.research.search_videos", return_value=entries), patch(
        "app.pipeline.steps.research.extract_metadata", return_value=_meta("v0", views=5)
    ):
        with pytest.raises(PipelineFailedError) as exc_info:
            ResearchSearchStep().run(ctx)
    assert exc_info.value.code == "no_results"


# ---------- captions step ----------

FAKE_VTT = """WEBVTT

00:00:00.000 --> 00:00:02.000
hello world this is a transcript
"""


def test_captions_step_saves_transcripts_and_records_skips(tmp_path):
    ctx, logs, _ = make_ctx(tmp_path, options={"research": {"query": "q"}})
    ctx.shared["research_videos"] = [
        _video(id="with_subs", has_manual_en=True, has_auto_en=False),
        _video(id="no_subs", has_manual_en=False, has_auto_en=False),
        _video(id="fails", has_manual_en=False, has_auto_en=True),
    ]

    def fake_download(url, dest_dir, video_id, *, manual, log):
        if video_id == "fails":
            raise YtDlpError("download_failed", "network timeout")
        path = dest_dir / f"{video_id}.en.vtt"
        path.write_text(FAKE_VTT)
        return path

    with patch("app.pipeline.steps.research.download_captions", side_effect=fake_download):
        ResearchCaptionsStep().run(ctx)

    results = ctx.shared["research_results"]
    by_id = {r["id"]: r for r in results}
    assert by_id["with_subs"]["transcript_file"] == "transcripts/with_subs.txt"
    assert by_id["with_subs"]["caption_source"] == "manual"
    assert by_id["no_subs"]["skipped_reason"] == "No captions available"
    assert by_id["fails"]["skipped_reason"] == "network timeout"
    saved = ctx.storage.get(ctx.job_relative("transcripts", "with_subs.txt")).read_text()
    assert "hello world this is a transcript" in saved


# ---------- manifest step ----------


def test_manifest_step_writes_research_manifest(tmp_path):
    ctx, _, _ = make_ctx(
        tmp_path,
        options={
            "research": {
                "query": "roblox tips",
                "result_count": 15,
                "sort_mode": "newest",
                "min_views": 1000,
                "uploaded_within_days": None,
                "max_duration_seconds": None,
            }
        },
    )
    ctx.shared["research_results"] = [
        {"id": "a", "title": "A", "transcript_file": "transcripts/a.txt", "caption_source": "auto"},
        {"id": "b", "title": "B", "skipped_reason": "No captions available"},
    ]

    ResearchManifestStep().run(ctx)

    manifest = json.loads(ctx.storage.get(ctx.job_relative("research-manifest.json")).read_bytes())
    assert manifest["query"] == "roblox tips"
    assert manifest["mode"] == "newest"
    assert manifest["filters"]["min_views"] == 1000
    assert len(manifest["videos"]) == 2
    assert manifest["videos"][1]["skipped_reason"] == "No captions available"
