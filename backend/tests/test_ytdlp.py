import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.utils.extraction_errors import classify as _classify_full
from app.utils.ytdlp import (
    YtDlpError,
    _run,
    _run_with_extractor_retry,
    extract_comments,
    extract_metadata,
    fetch_profile_reel_view_count,
    impersonation_available,
    update_to_channel,
)


def _classify(stderr: str, **kwargs) -> str:
    """Category only — these tests predate the structured Classification."""
    return _classify_full(stderr, **kwargs).code


def _completed(cmd, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _noop_log(level, message):
    pass


def test_classify_video_unavailable():
    assert _classify("ERROR: Private video. Sign in if you've been granted access.") == "tiktok_video_private"
    assert _classify("ERROR: [youtube] abc123: This video is not available in your country") == "tiktok_region_restricted"


def test_classify_extractor_outdated():
    assert _classify("ERROR: Unable to extract some info; please report this issue") == "yt_dlp_update_required"
    assert _classify("Unsupported URL: https://example.com/x") == "yt_dlp_update_required"


def test_classify_generic_download_failed():
    assert _classify("ERROR: connection reset by peer") == "tiktok_network_error"


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


def test_video_unavailable_is_terminal_and_never_retried_anonymously():
    """Retrying a private video without cookies just burns a request against
    a rate limiter — the video state is not an access problem."""
    fake_proc = _completed(["yt-dlp"], returncode=1, stderr=b"ERROR: Private video")
    with patch("app.utils.ytdlp._run", return_value=fake_proc) as mock_run, patch(
        "app.utils.ytdlp.cookies_configured", return_value=True
    ):
        with pytest.raises(YtDlpError) as exc_info:
            extract_metadata("https://youtu.be/private", log=_noop_log)
    assert exc_info.value.code == "tiktok_video_private"
    assert mock_run.call_count == 1


# ------------------------------------------------------- the attempt matrix


def test_bot_challenge_with_cookies_retries_once_anonymously_and_succeeds():
    """The reported failure shape: cookies in use, TikTok returns a page with
    no embedded data. If dropping the cookies fixes it, the cookies were the
    problem — and the log must say so instead of blaming the extractor."""
    blocked = _completed(
        ["yt-dlp"], returncode=1,
        stderr=b"ERROR: [TikTok] 123: Unable to extract universal data for rehydration",
    )
    success = _completed(["yt-dlp"], returncode=0, stdout=b'{"id": "123"}')
    calls = []

    def fake_run(args, timeout, *, use_cookies=True, impersonate=None):
        calls.append(use_cookies)
        return blocked if use_cookies else success

    logs = []
    with patch("app.utils.ytdlp._run", side_effect=fake_run), patch(
        "app.utils.ytdlp.cookies_configured", return_value=True
    ), patch("app.utils.ytdlp.time.sleep"):
        result = _run_with_extractor_retry(
            ["--dump-single-json", "url"], timeout=30, log=lambda l, m: logs.append((l, m))
        )

    assert calls == [True, False], "exactly one authenticated then one anonymous attempt"
    assert result is success
    assert any("cookies are stale or rejected" in m for _, m in logs)


def test_attempts_are_bounded_at_two():
    """TikTok rate-limits aggressively; hammering turns a recoverable failure
    into a durable block."""
    blocked = _completed(
        ["yt-dlp"], returncode=1,
        stderr=b"ERROR: [TikTok] 123: Unable to extract universal data for rehydration",
    )
    with patch("app.utils.ytdlp._run", return_value=blocked) as mock_run, patch(
        "app.utils.ytdlp.cookies_configured", return_value=True
    ), patch("app.utils.ytdlp.time.sleep"):
        with pytest.raises(YtDlpError):
            _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    assert mock_run.call_count == 2


def test_both_attempts_blocked_points_at_the_server_not_the_cookies():
    """Identical failure with and without cookies rules the cookies out. That
    conclusion is unavailable from stderr alone."""
    blocked = _completed(
        ["yt-dlp"], returncode=1,
        stderr=b"ERROR: [TikTok] 123: Unable to extract universal data for rehydration",
    )
    with patch("app.utils.ytdlp._run", return_value=blocked), patch(
        "app.utils.ytdlp.cookies_configured", return_value=True
    ), patch("app.utils.ytdlp.time.sleep"):
        with pytest.raises(YtDlpError) as exc_info:
            _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)

    assert exc_info.value.code == "tiktok_region_restricted"
    # The verdict must say the cookies are exonerated and name a way forward,
    # or the reader just retries from the same blocked address.
    assert "blocking this server's IP" in exc_info.value.message
    assert "TIKTOK_DEVICE_ID" in exc_info.value.message
    assert "YTDLP_PROXY" in exc_info.value.message


def test_no_anonymous_retry_when_no_cookies_were_used():
    """Without cookies there is nothing to drop, so a second identical attempt
    would be pure noise."""
    blocked = _completed(
        ["yt-dlp"], returncode=1,
        stderr=b"ERROR: [TikTok] 123: Unable to extract universal data for rehydration",
    )
    with patch("app.utils.ytdlp._run", return_value=blocked) as mock_run, patch(
        "app.utils.ytdlp.cookies_configured", return_value=False
    ):
        with pytest.raises(YtDlpError) as exc_info:
            _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    assert mock_run.call_count == 1
    assert exc_info.value.code == "tiktok_layout_changed"


def test_no_pip_install_ever_happens_during_a_job():
    """A mid-job update swapped the binary under running work and never fixed
    the failure that triggered it."""
    blocked = _completed(
        ["yt-dlp"], returncode=1,
        stderr=b"ERROR: Unable to extract; please report this issue. Confirm you are on "
               b"the latest version using yt-dlp -U",
    )
    with patch("app.utils.ytdlp._run", return_value=blocked), patch(
        "app.utils.ytdlp.cookies_configured", return_value=False
    ), patch("app.utils.ytdlp._pip_install") as mock_pip:
        with pytest.raises(YtDlpError):
            _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    mock_pip.assert_not_called()


def test_impersonation_is_forced_when_a_target_is_available():
    captured = {}

    def fake_run(args, timeout, *, use_cookies=True, impersonate=None):
        captured["impersonate"] = impersonate
        return _completed(["yt-dlp"], returncode=0, stdout=b"{}")

    with patch("app.utils.ytdlp._run", side_effect=fake_run), patch(
        "app.utils.ytdlp.impersonation_available", return_value=True
    ):
        _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    assert captured["impersonate"] == "chrome"


def test_impersonation_is_not_forced_when_unavailable():
    """Passing --impersonate with no curl_cffi makes yt-dlp exit immediately,
    turning a recoverable extraction into a hard failure."""
    captured = {}

    def fake_run(args, timeout, *, use_cookies=True, impersonate=None):
        captured["impersonate"] = impersonate
        return _completed(["yt-dlp"], returncode=0, stdout=b"{}")

    with patch("app.utils.ytdlp._run", side_effect=fake_run), patch(
        "app.utils.ytdlp.impersonation_available", return_value=False
    ):
        _run_with_extractor_retry(["--dump-single-json", "url"], timeout=30, log=_noop_log)
    assert captured["impersonate"] is None


def test_run_passes_impersonate_and_omits_cookies_when_asked(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    captured: list[str] = []

    def fake_subprocess_run(cmd, **kwargs):
        captured.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies)
    try:
        with patch("app.utils.ytdlp.subprocess.run", side_effect=fake_subprocess_run):
            _run(["--dump-single-json", "url"], timeout=30, use_cookies=False, impersonate="chrome")
    finally:
        settings.COOKIES_FILE = original

    assert "--cookies" not in captured
    assert captured[captured.index("--impersonate") + 1] == "chrome"


# ------------------------------------------------------------ channel update


def test_update_to_channel_uses_the_prerelease_flag_for_nightly():
    calls = []
    with patch("app.utils.ytdlp.get_version", side_effect=["2026.7.4", "2026.8.4.dev0"]), patch(
        "app.utils.ytdlp._pip_install",
        side_effect=lambda spec, timeout, pre=False: calls.append((spec, pre))
        or _completed(["pip"], returncode=0),
    ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
        result = update_to_channel("nightly", _noop_log)

    assert calls == [("yt-dlp[default,curl-cffi]", True)]
    assert result == {"ok": True, "channel": "nightly", "before": "2026.7.4", "after": "2026.8.4.dev0"}


def test_update_to_channel_stable_does_not_use_prerelease():
    calls = []
    with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
        "app.utils.ytdlp._pip_install",
        side_effect=lambda spec, timeout, pre=False: calls.append(pre)
        or _completed(["pip"], returncode=0),
    ), patch("app.utils.ytdlp.impersonation_available", return_value=True):
        update_to_channel("stable", _noop_log)
    assert calls == [False]


def test_update_failure_is_never_fatal():
    with patch("app.utils.ytdlp.get_version", return_value="2026.7.4"), patch(
        "app.utils.ytdlp._pip_install", return_value=None
    ):
        result = update_to_channel("stable", _noop_log)  # must not raise
    assert result["ok"] is False


def test_startup_update_is_skipped_unless_enabled():
    from app.utils.ytdlp import maybe_update_on_startup

    settings = get_settings()
    original = settings.YTDLP_UPDATE_ON_STARTUP
    settings.YTDLP_UPDATE_ON_STARTUP = False
    try:
        with patch("app.utils.ytdlp._pip_install") as mock_pip:
            assert maybe_update_on_startup(_noop_log) is None
        mock_pip.assert_not_called()
    finally:
        settings.YTDLP_UPDATE_ON_STARTUP = original


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


# ------------------------------------------------- TikTok mobile API path


def test_device_id_is_passed_to_yt_dlp_as_an_extractor_arg():
    """Without app info of some kind, yt-dlp never attempts TikTok's mobile
    API at all — it goes straight to the web page that is being blocked."""
    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    original = settings.TIKTOK_DEVICE_ID
    settings.TIKTOK_DEVICE_ID = "1234567890123456789"
    try:
        with patch("app.utils.ytdlp.subprocess.run", side_effect=fake_run):
            _run(["--dump-single-json", "url"], timeout=30)
    finally:
        settings.TIKTOK_DEVICE_ID = original

    assert "--extractor-args" in captured
    assert "tiktok:device_id=1234567890123456789" in captured


def test_no_extractor_args_are_passed_when_nothing_is_configured():
    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    originals = (settings.TIKTOK_DEVICE_ID, settings.YTDLP_EXTRACTOR_ARGS)
    settings.TIKTOK_DEVICE_ID = ""
    settings.YTDLP_EXTRACTOR_ARGS = ""
    try:
        with patch("app.utils.ytdlp.subprocess.run", side_effect=fake_run):
            _run(["--dump-single-json", "url"], timeout=30)
    finally:
        settings.TIKTOK_DEVICE_ID, settings.YTDLP_EXTRACTOR_ARGS = originals

    assert "--extractor-args" not in captured


def test_raw_extractor_args_passthrough_supports_several_specs():
    from app.utils.ytdlp import extractor_args

    settings = get_settings()
    originals = (settings.TIKTOK_DEVICE_ID, settings.YTDLP_EXTRACTOR_ARGS)
    settings.TIKTOK_DEVICE_ID = ""
    settings.YTDLP_EXTRACTOR_ARGS = "tiktok:app_info=123 ; youtube:player_client=web ;"
    try:
        flags = extractor_args()
    finally:
        settings.TIKTOK_DEVICE_ID, settings.YTDLP_EXTRACTOR_ARGS = originals

    assert flags == [
        "--extractor-args", "tiktok:app_info=123",
        "--extractor-args", "youtube:player_client=web",
    ], "blank segments from trailing/extra semicolons must not become empty flags"


def test_impersonation_probe_is_cached_not_re_run_per_extraction():
    """It is a property of the installed binary, consulted on every job."""
    from app.utils.ytdlp import impersonation_available as probe

    probe.cache_clear()
    listing = (
        b"[info] Available impersonate targets\n"
        b"Client          OS           Source\n"
        b"--------------------------------------\n"
        b"Chrome-133      Macos-15     curl_cffi\n"
    )
    with patch("app.utils.ytdlp._run", return_value=_completed(["yt-dlp"], 0, stdout=listing)) as m:
        assert probe() is True
        assert probe() is True
        assert probe() is True
    assert m.call_count == 1
    probe.cache_clear()


# ------------------------------------------------------ cookie diagnostics


def test_cookie_report_describes_shape_and_age_but_never_contents(tmp_path):
    from app.utils.ytdlp import cookie_file_report

    cookies = tmp_path / "cookies.txt"
    secret = "SUPERSECRETSESSIONVALUE"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        f".tiktok.com\tTRUE\t/\tTRUE\t1799999999\tsessionid\t{secret}\n"
        ".tiktok.com\tTRUE\t/\tTRUE\t1799999999\tttwid\tanother-value\n"
    )
    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies)
    try:
        report = cookie_file_report()
    finally:
        settings.COOKIES_FILE = original

    assert report["present"] is True
    assert report["netscape_format"] is True
    assert report["has_tiktok_entries"] is True
    assert report["tiktok_session_cookies_present"] == ["sessionid", "ttwid"]
    assert report["entry_count"] == 2
    assert report["modified_age_days"] < 1
    assert report["likely_stale"] is False
    # The whole point: no values anywhere in the payload.
    assert secret not in json.dumps(report)
    assert "another-value" not in json.dumps(report)


def test_cookie_report_flags_a_stale_export(tmp_path):
    import os
    import time as _time

    from app.utils.ytdlp import cookie_file_report

    cookies = tmp_path / "old.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t1\tsessionid\tv\n")
    old = _time.time() - 60 * 86400
    os.utime(cookies, (old, old))

    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies)
    try:
        report = cookie_file_report()
    finally:
        settings.COOKIES_FILE = original

    assert report["likely_stale"] is True
    assert report["modified_age_days"] >= 59


def test_cookie_report_when_the_path_is_wrong(tmp_path):
    from app.utils.ytdlp import cookie_file_report

    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(tmp_path / "nope.txt")
    try:
        report = cookie_file_report()
    finally:
        settings.COOKIES_FILE = original
    assert report == {
        "configured": True,
        "present": False,
        "reason": "COOKIES_FILE is set but no file exists at that path",
    }


def test_cookie_status_line_reports_age_so_staleness_is_visible_up_front(tmp_path):
    """A rejected session produces the same empty page as a bot-check, so the
    jar's age belongs in the log before anyone blames the extractor."""
    import os
    import time as _time

    from app.utils.ytdlp import cookies_status

    cookies = tmp_path / "c.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t1\tsessionid\tv\n")
    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies)
    try:
        assert "days old" in cookies_status()
        assert "may reject" not in cookies_status()

        old = _time.time() - 90 * 86400
        os.utime(cookies, (old, old))
        stale = cookies_status()
    finally:
        settings.COOKIES_FILE = original

    assert "90.0 days old" in stale
    assert "may reject it" in stale
