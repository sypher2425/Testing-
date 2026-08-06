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
    impersonation_available,
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


def test_self_update_warns_when_no_impersonation_target_is_available():
    """The TikTok "Unable to extract universal data for rehydration" failure
    looks like an outdated extractor, so it lands here — but updating never
    fixes it. The log must say what actually would."""
    logs = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
        "app.utils.ytdlp._pip_install", return_value=_completed(["pip"], returncode=0)
    ), patch("app.utils.ytdlp.impersonation_available", return_value=False):
        _self_update(lambda level, msg: logs.append((level, msg)))

    combined = " ".join(m for _, m in logs)
    assert "unchanged" in combined
    assert "curl_cffi" in combined
    assert any(level == "warning" and "impersonation" in msg for level, msg in logs)


def test_self_update_stays_quiet_about_impersonation_when_it_works():
    logs = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
        "app.utils.ytdlp._pip_install", return_value=_completed(["pip"], returncode=0)
    ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
        _self_update(lambda level, msg: logs.append((level, msg)))

    assert not any("curl_cffi" in m for _, m in logs)


def test_self_update_skips_the_nightly_channel_by_default():
    calls = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
        "app.utils.ytdlp._pip_install",
        side_effect=lambda spec, timeout, pre=False: calls.append(pre)
        or _completed(["pip"], returncode=0),
    ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
        _self_update(_noop_log)

    assert calls == [False], "nightly must be opt-in"


def test_self_update_tries_the_nightly_channel_when_enabled():
    """Extractor fixes for TikTok/Instagram land on nightly days before
    stable, so an operator can opt in when a platform breaks."""
    calls = []
    settings = get_settings()
    original = settings.YTDLP_ALLOW_NIGHTLY_UPDATE
    settings.YTDLP_ALLOW_NIGHTLY_UPDATE = True
    logs = []
    try:
        with patch(
            "app.utils.ytdlp.get_version", side_effect=["2026.7.4", "2026.7.4", "2026.8.4.234419"]
        ), patch(
            "app.utils.ytdlp._pip_install",
            side_effect=lambda spec, timeout, pre=False: calls.append(pre)
            or _completed(["pip"], returncode=0),
        ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
            _self_update(lambda level, msg: logs.append((level, msg)))
    finally:
        settings.YTDLP_ALLOW_NIGHTLY_UPDATE = original

    assert calls == [False, True], "stable first, then nightly"
    assert any("nightly" in m and "2026.8.4.234419" in m for _, m in logs)


def test_self_update_nightly_failure_is_not_fatal():
    settings = get_settings()
    original = settings.YTDLP_ALLOW_NIGHTLY_UPDATE
    settings.YTDLP_ALLOW_NIGHTLY_UPDATE = True
    logs = []
    try:
        with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
            "app.utils.ytdlp._pip_install",
            side_effect=lambda spec, timeout, pre=False: None if pre else _completed(["pip"], 0),
        ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
            _self_update(lambda level, msg: logs.append((level, msg)))  # must not raise
    finally:
        settings.YTDLP_ALLOW_NIGHTLY_UPDATE = original

    assert any("nightly update failed" in m.lower() for _, m in logs)


def test_impersonation_available_parses_real_target_listing():
    """Verbatim shape of `yt-dlp --list-impersonate-targets` with curl_cffi
    installed (captured from yt-dlp 2026.07.04)."""
    listing = (
        b"[info] Available impersonate targets\n"
        b"Client          OS           Source\n"
        b"--------------------------------------\n"
        b"Chrome-133      Macos-15     curl_cffi\n"
        b"Safari-18.0     Ios-18.0     curl_cffi\n"
    )
    with patch("app.utils.ytdlp._run", return_value=_completed(["yt-dlp"], 0, stdout=listing)):
        assert impersonation_available() is True


def test_impersonation_available_is_false_when_every_target_is_unavailable():
    """Without curl_cffi yt-dlp still lists targets — each marked unavailable."""
    listing = (
        b"[info] Available impersonate targets\n"
        b"Client          OS           Source\n"
        b"--------------------------------------\n"
        b"Chrome-133      Macos-15     (unavailable)\n"
        b"Safari-18.0     Ios-18.0     (unavailable)\n"
    )
    with patch("app.utils.ytdlp._run", return_value=_completed(["yt-dlp"], 0, stdout=listing)):
        assert impersonation_available() is False


def test_impersonation_available_never_raises():
    """It is a diagnostic; a broken probe must not break extraction."""
    with patch("app.utils.ytdlp._run", side_effect=YtDlpError("download_failed", "no yt-dlp")):
        assert impersonation_available() is False


def test_extract_comments_failure_returns_typed_status_not_bare_empty():
    logs = []
    with patch("app.utils.ytdlp._run", side_effect=RuntimeError("boom")):
        result = extract_comments("https://example.com/v", limit=100, log=lambda l, m: logs.append((l, m)))
    assert result["status"] == "extraction_failed"
    assert result["comments"] == []
    assert result["error"] == "boom"
    assert result["attempted_at"]
    assert any(l == "warning" for l, _ in logs)


def test_extract_comments_sorts_by_likes_caps_limit_and_counts_replies():
    info = {
        "comments": [
            {"id": "c1", "author": "a", "text": "hi", "like_count": 3, "timestamp": 1, "parent": "root"},
            {"id": "c2", "author": "b", "text": "great!", "like_count": 50, "timestamp": 2, "parent": "root"},
            {"id": "c3", "author": "c", "text": "meh", "like_count": 10, "timestamp": 3, "parent": "root"},
            {"id": "c4", "author": "d", "text": "reply", "like_count": 1, "timestamp": 4, "parent": "c2"},
        ]
    }
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps(info).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = extract_comments("https://example.com/v", limit=2, log=_noop_log)

    assert result["status"] == "success"
    assert result["extracted_comment_count"] == 2
    comments = result["comments"]
    assert comments[0]["like_count"] == 50
    assert comments[0]["reply_count"] == 1  # c4 replies to c2
    assert comments[1]["like_count"] == 10
    # Replies themselves are not listed as top-level comments.
    assert all(c["id"] != "c4" for c in comments)


def test_extract_comments_tiktok_is_unsupported_without_attempting():
    with patch("app.utils.ytdlp._run") as mock_run:
        result = extract_comments("https://tiktok.com/v", limit=10, log=_noop_log, platform="tiktok")
    assert result["status"] == "unsupported"
    mock_run.assert_not_called()


def test_extract_comments_zero_results_on_capable_platform_flagged_as_unexpected():
    """Instagram's public_comment_text capability is *false* in R1.5 too, so
    IG zero-results is 'unsupported'. We use a platform that DOES have the
    capability (YouTube) to test the unexpected_empty_result branch."""
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps({"comments": []}).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = extract_comments(
            "https://youtu.be/x", limit=10, log=_noop_log,
            platform="youtube", platform_comment_count=378,
        )
    assert result["status"] == "unexpected_empty_result"
    assert result["reason"] == "zero_results_unexpected"
    assert result["platform_comment_count"] == 378


def test_extract_comments_zero_results_on_incapable_platform_flagged_as_unsupported():
    """R1.5: Instagram (public_comment_text=false in capabilities) with a
    non-zero platform count and empty extraction is 'unsupported', not a
    failure to fetch — the extractor simply doesn't do that job."""
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps({"comments": []}).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = extract_comments(
            "https://instagram.com/reel/x", limit=10, log=_noop_log,
            platform="instagram", platform_comment_count=378,
        )
    assert result["status"] == "unsupported"
    assert "not supported" in result["reason"].lower()


def test_extract_comments_auth_and_rate_limit_classification():
    auth_proc = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: login required to view comments")
    with patch("app.utils.ytdlp._run", return_value=auth_proc):
        result = extract_comments("https://instagram.com/reel/x", limit=10, log=_noop_log, platform="instagram")
    assert result["status"] == "authentication_required"

    rl_proc = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: rate-limit reached, try again later")
    with patch("app.utils.ytdlp._run", return_value=rl_proc):
        result = extract_comments("https://instagram.com/reel/x", limit=10, log=_noop_log, platform="instagram")
    assert result["status"] == "rate_limited"


def test_extract_comments_platform_count_zero_uses_no_comments_status():
    """R1.5: platform reports 0 comments → dedicated no_comments status."""
    fake_proc = _completed(["yt-dlp"], returncode=0, stdout=json.dumps({"comments": []}).encode())
    with patch("app.utils.ytdlp._run", return_value=fake_proc):
        result = extract_comments(
            "https://youtu.be/x", limit=10, log=_noop_log, platform="youtube", platform_comment_count=0
        )
    assert result["status"] == "no_comments"


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
