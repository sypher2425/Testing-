"""Failure classification and the extraction diagnostics endpoint.

These exist because "the extraction failed" was not a diagnosis: every TikTok
failure carries yt-dlp's "confirm you are on the latest version" boilerplate,
and taking it literally labelled a current version as outdated.
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils import extraction_errors as ee

REHYDRATION = (
    "ERROR: [TikTok] 7666486121907358978: Unable to extract universal data for "
    "rehydration; please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
    "?q= , filling out the appropriate issue template. Confirm you are on the latest "
    "version using yt-dlp -U"
)


@pytest.fixture()
def client():
    return TestClient(app)


# --------------------------------------------------------- classification


def test_the_reported_failure_is_not_called_outdated():
    """The exact stderr from the reported job."""
    assert ee.classify(REHYDRATION).code == ee.LAYOUT_CHANGED
    assert ee.classify(REHYDRATION).code != "extractor_outdated"


def test_the_same_page_means_something_different_with_cookies_in_play():
    """A cookie jar TikTok rejects produces the same empty page as no cookies,
    so authenticated context reads as a challenge, not a layout change."""
    assert ee.classify(REHYDRATION, used_cookies=True).code == ee.BOT_CHALLENGE
    assert ee.classify(REHYDRATION, used_cookies=False).code == ee.LAYOUT_CHANGED


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("ERROR: Private video", ee.VIDEO_PRIVATE),
        ("ERROR: Video unavailable", ee.VIDEO_UNAVAILABLE),
        ("ERROR: This video has been removed by the uploader", ee.VIDEO_UNAVAILABLE),
        ("ERROR: Your IP address is blocked from accessing this post", ee.REGION_RESTRICTED),
        ("ERROR: not available in your country", ee.REGION_RESTRICTED),
        ("ERROR: HTTP Error 429: Too Many Requests", ee.RATE_LIMITED),
        ("ERROR: login required to view this post", ee.LOGIN_REQUIRED),
        ("ERROR: Unable to extract challenge data", ee.BOT_CHALLENGE),
        ("ERROR: connection reset by peer", ee.NETWORK_ERROR),
        ("yt-dlp is not installed", ee.DEPENDENCY_MISSING),
        ("ERROR: Unsupported URL: https://example.com/x", ee.UPDATE_REQUIRED),
        ("ERROR: something nobody has seen before", ee.UNKNOWN),
    ],
)
def test_category_markers(stderr, expected):
    assert ee.classify(stderr).code == expected


def test_a_private_video_is_never_reclassified_by_the_boilerplate():
    """Private videos carry the same 'confirm you are on the latest version'
    tail — specific markers must win over the generic one."""
    stderr = "ERROR: [TikTok] 1: Private video. Confirm you are on the latest version using yt-dlp -U"
    assert ee.classify(stderr).code == ee.VIDEO_PRIVATE
    assert ee.classify(stderr).terminal is True


def test_reconcile_does_not_invent_an_ip_ban_from_two_failures():
    """Both attempts failing proves the cookies are not the sole cause. It
    does not prove the IP is banned — yt-dlp never said so. Asserting it once
    sent a user on a home connection shopping for a residential proxy."""
    auth = ee.classify(REHYDRATION, used_cookies=True)
    anon = ee.classify(REHYDRATION, used_cookies=False)
    verdict = ee.reconcile(auth, anon)
    assert verdict.code == ee.EXTRACTION_BLOCKED
    assert verdict.code != ee.REGION_RESTRICTED


def test_region_restricted_is_only_used_when_yt_dlp_actually_says_so():
    assert ee.classify("ERROR: Your IP address is blocked from accessing this post").code == (
        ee.REGION_RESTRICTED
    )
    assert ee.classify("ERROR: not available in your country").code == ee.REGION_RESTRICTED


def test_blocked_verdict_ranks_remedies_cheapest_first():
    """Someone reading this should try a free cookie re-export before paying
    for a proxy, so the order in the sentence matters."""
    message = ee.message_for(ee.EXTRACTION_BLOCKED)
    assert message.index("cookie export is stale") < message.index("TIKTOK_DEVICE_ID")
    assert message.index("TIKTOK_DEVICE_ID") < message.index("YTDLP_PROXY")


def test_reconcile_prefers_a_terminal_video_state():
    auth = ee.classify(REHYDRATION, used_cookies=True)
    anon = ee.classify("ERROR: Private video")
    assert ee.reconcile(auth, anon).code == ee.VIDEO_PRIVATE


def test_every_category_has_an_actionable_message():
    for code in (
        ee.LAYOUT_CHANGED, ee.BOT_CHALLENGE, ee.COOKIE_INVALID, ee.LOGIN_REQUIRED,
        ee.VIDEO_PRIVATE, ee.VIDEO_UNAVAILABLE, ee.REGION_RESTRICTED, ee.RATE_LIMITED,
        ee.NETWORK_ERROR, ee.DEPENDENCY_MISSING, ee.UPDATE_REQUIRED, ee.UNKNOWN,
        ee.EXTRACTION_BLOCKED,
    ):
        message = ee.message_for(code)
        assert len(message) > 30
        # No error code leakage and no bare "try again".
        assert code not in message


def test_blocked_categories_offer_the_manual_upload_route():
    """The user should never be stuck when TikTok is the thing that broke."""
    for code in (
        ee.LAYOUT_CHANGED, ee.BOT_CHALLENGE, ee.REGION_RESTRICTED,
        ee.RATE_LIMITED, ee.EXTRACTION_BLOCKED,
    ):
        assert code in ee.OFFER_MANUAL_UPLOAD
        assert "upload" in ee.message_for(code).lower()
    # A private video cannot be fixed by uploading someone else's file.
    assert ee.VIDEO_PRIVATE not in ee.OFFER_MANUAL_UPLOAD


# ------------------------------------------------------------ diagnostics


def test_extraction_health_reports_the_whole_environment(client):
    resp = client.get("/api/health/extraction")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"yt_dlp", "impersonation", "ffmpeg", "cookies", "proxy", "tiktok"}
    assert "version" in body["yt_dlp"]
    assert "channel" in body["yt_dlp"]
    assert "available" in body["impersonation"]
    assert "ffmpeg" in body["ffmpeg"] and "ffprobe" in body["ffmpeg"]


def test_extraction_health_never_leaks_cookie_contents(client, tmp_path):
    from app.config import get_settings

    cookies = tmp_path / "cookies.txt"
    secret = "TOTALLYSECRETSESSIONID"
    cookies.write_text(
        f"# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tTRUE\t1\tsessionid\t{secret}\n"
    )
    settings = get_settings()
    original = settings.COOKIES_FILE
    settings.COOKIES_FILE = str(cookies)
    try:
        body = client.get("/api/health/extraction").text
    finally:
        settings.COOKIES_FILE = original

    assert secret not in body
    assert json.loads(body)["cookies"]["has_tiktok_entries"] is True


def test_extraction_health_survives_a_missing_yt_dlp(client):
    """A missing binary is a finding to report, not a 500."""
    with patch("app.utils.ytdlp.get_version", side_effect=RuntimeError("yt-dlp is not installed")):
        resp = client.get("/api/health/extraction")
    assert resp.status_code == 200
    assert resp.json()["yt_dlp"]["installed"] is False


# ------------------------------------------------------------------ proxy


def test_proxy_credentials_are_redacted_everywhere():
    """A proxy URL routinely carries a password, and it surfaces in the job
    log, the diagnostics endpoint and error detail."""
    from app.utils.ytdlp import redact_proxy

    assert redact_proxy("http://user:s3cret@proxy.example:8080") == "http://***@proxy.example:8080"
    assert "s3cret" not in redact_proxy("http://user:s3cret@proxy.example:8080")
    assert redact_proxy("socks5://u:p@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"
    # No credentials to hide -> shown as-is, which is useful for debugging.
    assert redact_proxy("http://proxy.example:8080") == "http://proxy.example:8080"
    assert redact_proxy("") == ""
    # Unparseable but non-empty must never fall through as the raw value.
    assert redact_proxy("not-a-url") == "(configured)"


def test_proxy_is_passed_to_yt_dlp_when_configured():
    """A platform blocking the server's IP cannot be worked around from that
    IP; routing elsewhere is the only real fix."""
    import subprocess
    from unittest.mock import patch as _patch

    from app.config import get_settings
    from app.utils.ytdlp import _run

    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    original = settings.YTDLP_PROXY
    settings.YTDLP_PROXY = "http://user:pw@proxy.example:8080"
    try:
        with _patch("app.utils.ytdlp.subprocess.run", side_effect=fake_run):
            _run(["--dump-single-json", "url"], timeout=30)
    finally:
        settings.YTDLP_PROXY = original

    assert "--proxy" in captured
    assert captured[captured.index("--proxy") + 1] == "http://user:pw@proxy.example:8080"


def test_no_proxy_flag_when_unset():
    import subprocess
    from unittest.mock import patch as _patch

    from app.config import get_settings
    from app.utils.ytdlp import _run

    captured: list[str] = []

    def fake_run(cmd, **kwargs):
        captured.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"{}", stderr=b"")

    settings = get_settings()
    original = settings.YTDLP_PROXY
    settings.YTDLP_PROXY = ""
    try:
        with _patch("app.utils.ytdlp.subprocess.run", side_effect=fake_run):
            _run(["--dump-single-json", "url"], timeout=30)
    finally:
        settings.YTDLP_PROXY = original

    assert "--proxy" not in captured


def test_blocked_message_names_the_levers_that_exist(client):
    """"Upload it instead" was the only advice, which is a dead end for
    someone who wants links to work."""
    message = ee.message_for(ee.EXTRACTION_BLOCKED)
    assert "TIKTOK_DEVICE_ID" in message
    assert "YTDLP_PROXY" in message
    assert "upload" in message.lower()


def test_diagnostics_reports_proxy_without_leaking_the_password(client, tmp_path):
    from app.config import get_settings

    settings = get_settings()
    original = settings.YTDLP_PROXY
    settings.YTDLP_PROXY = "http://user:hunter2@proxy.example:8080"
    try:
        body = client.get("/api/health/extraction").text
    finally:
        settings.YTDLP_PROXY = original

    assert "hunter2" not in body
    assert "proxy.example" in body
