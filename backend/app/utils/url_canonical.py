"""Per-platform URL canonicalization + platform post ID extraction.

Round 1 stored `source_url` raw (tracking params and all), so two links to
the same TikTok post via different share URLs looked like different posts.
This module strips tracking noise, keeps the identifying path bits, and
extracts the platform-native post ID. Unknown formats are left alone
(never destructive).
"""
import re
from dataclasses import dataclass
from urllib.parse import urlparse

_TIKTOK_VIDEO_ID = re.compile(r"/video/(\d+)")
_TIKTOK_HANDLE = re.compile(r"/(@[^/]+)/")
_TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}

_INSTAGRAM_SHORTCODE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")

_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_WATCH = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
_YOUTUBE_PATH = re.compile(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})")


@dataclass
class CanonicalUrl:
    original: str
    canonical: str  # equal to original if unknown format — never destructive
    platform: str  # "tiktok" | "instagram" | "youtube" | "unknown"
    post_id: str | None


def canonicalize(url: str) -> CanonicalUrl:
    if not url or not isinstance(url, str):
        return CanonicalUrl(original=url or "", canonical=url or "", platform="unknown", post_id=None)

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "tiktok.com" in host:
        return _canonicalize_tiktok(url, host, path)
    if "instagram.com" in host:
        return _canonicalize_instagram(url, path)
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.strip("/")
        if _YOUTUBE_ID.match(video_id):
            return CanonicalUrl(url, f"https://www.youtube.com/watch?v={video_id}", "youtube", video_id)
        return CanonicalUrl(url, url, "youtube", None)
    if "youtube.com" in host:
        # `v=` may appear in the query (watch URLs) or embedded in the path
        # (shorts/embed/live/v). Search for the id in a combined "path + ?query"
        # string so both spellings are handled by a single check.
        combined = f"{path}?{parsed.query}" if parsed.query else path
        match = _YOUTUBE_WATCH.search(combined) or _YOUTUBE_PATH.search(path)
        if match:
            video_id = match.group(1)
            return CanonicalUrl(url, f"https://www.youtube.com/watch?v={video_id}", "youtube", video_id)
        return CanonicalUrl(url, url, "youtube", None)

    return CanonicalUrl(url, url, "unknown", None)


def _canonicalize_tiktok(url: str, host: str, path: str) -> CanonicalUrl:
    # Short-link hosts (vm.tiktok.com/XXXX, vt.tiktok.com/XXXX) don't carry
    # the post id in the URL itself — we can't canonicalize without a
    # network follow, so leave the URL alone but still identify the platform.
    if host in _TIKTOK_SHORT_HOSTS:
        return CanonicalUrl(url, f"https://{host}{path}", "tiktok", None)

    video_match = _TIKTOK_VIDEO_ID.search(path)
    handle_match = _TIKTOK_HANDLE.search(path)
    if video_match and handle_match:
        handle = handle_match.group(1)
        video_id = video_match.group(1)
        canonical = f"https://www.tiktok.com/{handle}/video/{video_id}"
        return CanonicalUrl(url, canonical, "tiktok", video_id)
    if video_match:
        video_id = video_match.group(1)
        canonical = f"https://www.tiktok.com/video/{video_id}"
        return CanonicalUrl(url, canonical, "tiktok", video_id)
    return CanonicalUrl(url, url, "tiktok", None)


def _canonicalize_instagram(url: str, path: str) -> CanonicalUrl:
    match = _INSTAGRAM_SHORTCODE.search(path)
    if not match:
        return CanonicalUrl(url, url, "instagram", None)
    shortcode = match.group(1)
    # Normalize the "reels" alias to "reel"; keep p/ and tv/ as-is.
    kind = "reel" if "/reel" in path else ("tv" if "/tv/" in path else "p")
    canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
    return CanonicalUrl(url, canonical, "instagram", shortcode)
