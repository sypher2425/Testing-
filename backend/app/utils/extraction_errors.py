"""Classification of yt-dlp extraction failures into actionable categories.

The point of this module is that "the extraction failed" is not a diagnosis.
yt-dlp funnels wildly different situations — a deleted video, a bot-check
page, an expired cookie jar, a rate limit — into stderr text that often ends
with the same "Confirm you are on the latest version using yt-dlp -U"
boilerplate. Taking that advice literally is what produced a job labelled
`extractor_outdated` on a version that was already current.

Classification therefore reads the *specific* markers first and falls back to
generic ones last, and the anonymous-retry outcome is fed in as evidence:
if a request fails with cookies and succeeds without them, the cookies are
the problem, and no amount of reading stderr would have told us that.
"""
from dataclasses import dataclass

# --- categories -------------------------------------------------------------
LAYOUT_CHANGED = "tiktok_layout_changed"
BOT_CHALLENGE = "tiktok_bot_challenge"
COOKIE_INVALID = "tiktok_cookie_invalid"
LOGIN_REQUIRED = "tiktok_login_required"
VIDEO_PRIVATE = "tiktok_video_private"
VIDEO_UNAVAILABLE = "tiktok_video_unavailable"
REGION_RESTRICTED = "tiktok_region_restricted"
RATE_LIMITED = "tiktok_rate_limited"
#: Both the authenticated and the anonymous attempt were refused. That rules
#: out the cookies as the *sole* cause but says nothing about why -- which is
#: exactly why it is not REGION_RESTRICTED. Claiming an IP ban from this
#: evidence sent a user shopping for a residential proxy they did not need.
EXTRACTION_BLOCKED = "tiktok_extraction_blocked"
NETWORK_ERROR = "tiktok_network_error"
DEPENDENCY_MISSING = "yt_dlp_dependency_missing"
UPDATE_REQUIRED = "yt_dlp_update_required"
UNKNOWN = "source_extraction_unknown"

#: Categories where retrying anonymously could plausibly help. Used to decide
#: whether attempt 2 is worth making at all — retrying a deleted video without
#: cookies just burns another request against a rate limiter.
RETRY_ANONYMOUSLY = frozenset({BOT_CHALLENGE, LAYOUT_CHANGED, COOKIE_INVALID, LOGIN_REQUIRED})

#: Categories the user can act on by uploading the file themselves. Drives the
#: "upload it directly" call to action in the UI.
OFFER_MANUAL_UPLOAD = frozenset(
    {
        LAYOUT_CHANGED, BOT_CHALLENGE, COOKIE_INVALID, LOGIN_REQUIRED,
        REGION_RESTRICTED, RATE_LIMITED, EXTRACTION_BLOCKED,
    }
)


@dataclass(frozen=True)
class Classification:
    code: str
    message: str
    #: True when the situation is about the *video* rather than our access to
    #: it — retrying with different credentials cannot help.
    terminal: bool = False


_MESSAGES = {
    LAYOUT_CHANGED: (
        "TikTok changed its page format and the current extractor could not read it. "
        "The source could not be downloaded automatically — upload the video file "
        "directly to continue."
    ),
    BOT_CHALLENGE: (
        "TikTok's JavaScript challenge could not be solved — it served a page whose "
        "challenge format this yt-dlp does not recognise. Refreshing cookies will not "
        "help: this happens before they are used. Two things do. Set TIKTOK_DEVICE_ID "
        "to use TikTok's mobile API, which skips the challenge entirely. Or set "
        "YTDLP_CHANNEL=nightly with YTDLP_UPDATE_ON_STARTUP=true — challenge-solving "
        "fixes land on nightly first, and this is the part of the extractor TikTok "
        "changes most often. Uploading the file directly always works."
    ),
    COOKIE_INVALID: (
        "The saved TikTok session may have expired. Replace it with a fresh cookie "
        "export, or upload the video directly."
    ),
    LOGIN_REQUIRED: (
        "TikTok requires a signed-in session for this video. Provide a fresh cookie "
        "export, or upload the video directly."
    ),
    VIDEO_PRIVATE: "This TikTok video is private, so it cannot be downloaded.",
    VIDEO_UNAVAILABLE: (
        "This TikTok video may be removed, restricted, or unavailable from the "
        "server's region."
    ),
    REGION_RESTRICTED: (
        "TikTok says this video is unavailable from this server's region or IP. "
        "Routing through another IP (YTDLP_PROXY) or uploading the file directly are "
        "the ways past it."
    ),
    EXTRACTION_BLOCKED: (
        "TikTok refused both an authenticated and an anonymous request, so the saved "
        "cookies are not the whole story. Most likely causes, cheapest first: the "
        "cookie export is stale (re-export it while signed in), TikTok's anti-bot "
        "rejected the request (set TIKTOK_DEVICE_ID to use its mobile API instead of "
        "the web page), or the server's IP is blocked — common on a VPS or cloud host, "
        "rare on a home connection, and fixed with YTDLP_PROXY. Uploading the file "
        "directly always works."
    ),
    RATE_LIMITED: (
        "TikTok is rate-limiting this server. Wait a few minutes before retrying, or "
        "upload the video directly."
    ),
    NETWORK_ERROR: "The server could not reach TikTok. Check the worker's network access.",
    DEPENDENCY_MISSING: (
        "A required extraction dependency is missing on the server. Check the "
        "extraction diagnostics endpoint."
    ),
    UPDATE_REQUIRED: (
        "yt-dlp could not parse this response and may genuinely need updating. See "
        "the extraction diagnostics endpoint for the installed version and channel."
    ),
    UNKNOWN: "The source could not be downloaded. Upload the video file directly to continue.",
}

# Ordered most-specific first. The generic "unable to extract ... yt-dlp -U"
# boilerplate is deliberately last: almost every TikTok failure carries it.
_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    (VIDEO_PRIVATE, ("private video", "account is private", "this post is private")),
    (
        VIDEO_UNAVAILABLE,
        (
            "video unavailable",
            "video has been removed",
            "removed by the uploader",
            "this video is no longer available",
            "content isn't available",
            "content is not available",
            "video not available",
        ),
    ),
    (
        REGION_RESTRICTED,
        (
            "geo restricted",
            "not available in your country",
            "your ip address is blocked",
            "blocked from accessing this post",
        ),
    ),
    (RATE_LIMITED, ("rate-limit", "rate limit", "too many requests", "http error 429")),
    (
        LOGIN_REQUIRED,
        ("login required", "sign in to confirm", "requires authentication", "log in to"),
    ),
    (
        # TikTok's JS challenge flow, from
        # TikTokBaseIE._solve_challenge_and_set_cookies: the page carried
        # neither the challenge element nor the "Please wait..." interstitial.
        # Cookies are irrelevant here — the failure happens before they are
        # used, which is why refreshing them changes nothing.
        BOT_CHALLENGE,
        (
            "unable to extract challenge data",
            "unexpected response from webpage request",
            "please wait...",
            "captcha",
            "verify to continue",
            "security check",
        ),
    ),
    (COOKIE_INVALID, ("cookies are no longer valid", "invalid cookie", "cookie has expired")),
    (
        NETWORK_ERROR,
        ("connection reset", "connection refused", "temporary failure in name resolution",
         "timed out", "unable to connect to proxy", "network is unreachable"),
    ),
    (DEPENDENCY_MISSING, ("yt-dlp is not installed", "no such file or directory: 'yt-dlp'")),
    # The rehydration failure: TikTok's page carried no embedded data. Kept
    # after the specific markers so a private/removed video is never mislabelled.
    (LAYOUT_CHANGED, ("universal data for rehydration", "unable to extract webpage video data")),
]

_TERMINAL = {VIDEO_PRIVATE, VIDEO_UNAVAILABLE}


def classify(stderr: str, *, used_cookies: bool = False) -> Classification:
    """Map yt-dlp stderr to a category.

    `used_cookies` only refines the reading: the same bot-check page means
    "your session is stale" when authenticated and "TikTok is blocking this
    server" when anonymous. It never overrides a terminal video state.
    """
    lowered = (stderr or "").lower()

    for code, markers in _MARKERS:
        if any(marker in lowered for marker in markers):
            if code is LAYOUT_CHANGED and used_cookies:
                # A cookie jar that TikTok rejects produces exactly the same
                # empty page as no cookies at all, so this is a candidate
                # rather than a conclusion — the anonymous retry decides.
                return Classification(BOT_CHALLENGE, _MESSAGES[BOT_CHALLENGE])
            return Classification(code, _MESSAGES[code], terminal=code in _TERMINAL)

    if "unable to extract" in lowered or "unsupported url" in lowered:
        return Classification(UPDATE_REQUIRED, _MESSAGES[UPDATE_REQUIRED])
    return Classification(UNKNOWN, _MESSAGES[UNKNOWN])


def reconcile(authenticated: Classification, anonymous: Classification | None) -> Classification:
    """Combine the two attempts into one verdict.

    This is the part stderr alone cannot give you: identical failures with and
    without cookies rule the cookies out, which turns a vague "maybe your
    session expired" into a specific statement about the server's IP.
    """
    if anonymous is None:
        return authenticated
    if anonymous.terminal:
        return anonymous
    if authenticated.code in (BOT_CHALLENGE, LAYOUT_CHANGED) and anonymous.code in (
        BOT_CHALLENGE, LAYOUT_CHANGED, UNKNOWN
    ):
        # Both refused. That exonerates the cookies as the sole cause, and
        # nothing more: yt-dlp never said "IP blocked", so neither do we. An
        # earlier version asserted a ban here and pointed at paid proxies,
        # which was wrong for anyone on a home connection.
        return Classification(EXTRACTION_BLOCKED, _MESSAGES[EXTRACTION_BLOCKED])
    return anonymous


def message_for(code: str) -> str:
    return _MESSAGES.get(code, _MESSAGES[UNKNOWN])
