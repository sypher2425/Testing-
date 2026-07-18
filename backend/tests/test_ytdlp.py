import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.utils.ytdlp import (
    YtDlpError,
    _classify,
    _run,
    _run_with_extractor_retry,
    _self_update,
    extract_comments,
    extract_metadata,
    fetch_profile_reel_view_count,
)


def _completed(cmd, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _noop_log(level, message):
    pass


def test_classify_video_unavailable():
    assert _classify("ERROR: Private video. Sign in if you've been granted access.") == "video_unavailable"
    assert _classify("ERROR: [youtube] abc123: This video is not available in your country") == "video_unavailable"


def test_classify_extractor_outdated():
    assert _classify("ERROR: Unable to extract some info; please report this issue") == "extractor_outdated"
    assert _classify("Unsupported URL: https://example.com/x") == "extractor_outdated"


def test_classify_generic_download_failed():
    assert _classify("ERROR: connection reset by peer") == "download_failed"


def test_extract_metadata_success():
    info = {
        "extractor_key": "Youtube",
        "title": "A cool video #outdoors",
        "description": "check this out #hiking #nature",
        "uploader": "SomeChannel",
        "upload_date": "20240115",
        "view_count": 1000,
        "like_count": 50,
        "comment_count": 5,
        "tags": ["outdoors"],
        "duration": 12.5,
        "ext": "mp4",
        "filesize": 123456,
    }
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps(info).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        meta = extract_metadata("https://youtu.be/xyz", log=_noop_log)

    assert meta.platform == "youtube"
    assert meta.title == "A cool video #outdoors"
    assert meta.upload_date == "2024-01-15"
    assert meta.view_count == 1000
    assert set(meta.hashtags) == {"outdoors", "hiking", "nature"}
    assert meta.filesize_approx == 123456


def test_extract_metadata_video_unavailable_does_not_trigger_self_update():
    fake_proc = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: Private video")
    with patch("app.utils.ytdlp._run", return_value=fake_proc), patch(
        "app.utils.ytdlp._self_update"
    ) as mock_update:
        with pytest.raises(YtDlpError) as exc_info:
            extract_metadata("https://youtu.be/private", log=_noop_log)
    assert exc_info.value.code == "video_unavailable"
    mock_update.assert_not_called()


def test_run_with_extractor_retry_self_updates_and_succeeds_on_retry():
    first_fail = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: Unable to extract; please report this issue")
    second_success = _completed(["yt-dlp"], returncode=0, stdout=b'{"ok": true}')

    with patch("app.utils.ytdlp._run", side_effect=[first_fail, second_success]), patch(
        "app.utils.ytdlp._self_update"
    ) as mock_update:
        result = _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)

    assert result is second_success
    mock_update.assert_called_once()


def test_run_with_extractor_retry_gives_up_after_failed_retry():
    fail = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: Unable to extract; please report this issue")

    with patch("app.utils.ytdlp._run", side_effect=[fail, fail]), patch("app.utils.ytdlp._self_update"):
        with pytest.raises(YtDlpError) as exc_info:
            _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    assert exc_info.value.code == "extractor_outdated"


def test_self_update_success_logs_old_and_new_version():
    logs = []
    with patch("app.utils.ytdlp.get_version", side_effect=["2026.1.1", "2026.7.4"]), patch(
        "app.utils.ytdlp.subprocess.run",
        return_value=_completed(["pip"], returncode=0),
    ):
        _self_update(lambda level, msg: logs.append((level, msg)))

    combined = " ".join(m for _, m in logs)
    assert "2026.1.1" in combined
    assert "2026.7.4" in combined


def test_self_update_pip_failure_falls_back_without_raising():
    logs = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.1.1"), patch(
        "app.utils.ytdlp.subprocess.run",
        return_value=_completed(["pip"], returncode=1, stderr=b"network error"),
    ):
        _self_update(lambda level, msg: logs.append((level, msg)))  # must not raise

    assert any("failed" in m.lower() for _, m in logs)


def test_self_update_pip_timeout_falls_back_without_raising():
    logs = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.1.1"), patch(
        "app.utils.ytdlp.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pip"], timeout=120),
    ):
        _self_update(lambda level, msg: logs.append((level, msg)))  # must not raise

    assert any("failed" in m.lower() for _, m in logs)


def test_extract_comments_best_effort_returns_empty_on_failure():
    logs = []
    with patch("app.utils.ytdlp._run", side_effect=RuntimeError("boom")):
        result = extract_comments("https://example.com/v", limit=100, log=lambda l, m: logs.append((l, m)))
    assert result == []
    assert any(l == "warning" for l, _ in logs)


def test_extract_comments_sorts_by_likes_and_caps_limit():
    info = {
        "comments": [
            {"author": "a", "text": "hi", "like_count": 3, "timestamp": 1},
            {"author": "b", "text": "great!", "like_count": 50, "timestamp": 2},
            {"author": "c", "text": "meh", "like_count": 10, "timestamp": 3},
        ]
    }
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps(info).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = extract_comments("https://example.com/v", limit=2, log=_noop_log)

    assert len(result) == 2
    assert result[0]["like_count"] == 50
    assert result[1]["like_count"] == 10


def test_run_copies_cookies_to_scratch_and_leaves_original_untouched(tmp_path):
    """yt-dlp writes an updated cookie jar back to whatever path it's given
    on exit. The real cookies file is mounted read-only by design, so _run
    must hand yt-dlp a scratch copy instead of the original path — this is
    the regression test for the "Read-only file system" crash that broke
    every cookie-authenticated fetch."""
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\noriginal-content\n")

    captured_cmd: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies_file)
    try:
        with patch("app.utils.ytdlp.subprocess.run", side_effect=fake_subprocess_run):
            _run(["--dump-single-json", "url"], timeout=30)
    finally:
        settings.COOKIES_FILE = original

    assert "--cookies" in captured_cmd
    passed_path = captured_cmd[captured_cmd.index("--cookies") + 1]
    assert passed_path != str(cookies_file), "must not hand yt-dlp the read-only mounted path directly"
    assert not Path(passed_path).exists(), "scratch cookies file must be cleaned up after the call"
    assert cookies_file.read_text() == "# Netscape HTTP Cookie File\noriginal-content\n"


def test_fetch_profile_reel_view_count_finds_matching_entry():
    info = {
        "entries": [
            {"id": "111", "view_count": 500},
            {"id": "222", "view_count": 7929},
            {"id": "333", "view_count": 402000},
        ]
    }
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps(info).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = fetch_profile_reel_view_count("someaccount", "222", log=_noop_log)
    assert result == 7929


def test_fetch_profile_reel_view_count_missing_entry_returns_none():
    info = {"entries": [{"id": "111", "view_count": 500}]}
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps(info).encode())
    logs = []
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = fetch_profile_reel_view_count(
            "someaccount", "999", log=lambda l, m: logs.append((l, m))
        )
    assert result is None
    assert any(l == "warning" for l, _ in logs)


def test_fetch_profile_reel_view_count_never_raises_on_failure():
    with patch("app.utils.ytdlp._run", side_effect=RuntimeError("network blip")):
        result = fetch_profile_reel_view_count("someaccount", "222", log=_noop_log)
    assert result is None


def test_fetch_profile_reel_view_count_no_username_returns_none_without_calling_ytdlp():
    with patch("app.utils.ytdlp._run") as mock_run:
        result = fetch_profile_reel_view_count("", "222", log=_noop_log)
    assert result is None
    mock_run.assert_not_called()
