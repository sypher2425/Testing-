"""Per-platform capability records: what the *current* extractor can and
cannot get. These describe our extractor, not universal claims about the
platform itself — so if we later add cookie-authenticated Instagram comment
extraction, we update this file, not the platform.
"""

_CAPABILITIES: dict[str, dict] = {
    "youtube": {
        "public_views": True,
        "public_likes": True,
        "public_shares": False,  # no such public metric on YouTube
        "public_comment_count": True,
        "public_comment_text": True,
        "public_hashtags": True,
        "public_description": True,
        "private_retention_requires_manual_import": True,
        "demographics_requires_manual_import": True,
    },
    "tiktok": {
        "public_views": True,
        "public_likes": True,
        "public_shares": True,  # repost_count
        "public_comment_count": True,
        "public_comment_text": False,  # yt-dlp extractor doesn't expose bodies
        "public_hashtags": True,
        "public_description": True,
        "private_retention_requires_manual_import": True,
        "demographics_requires_manual_import": True,
    },
    "instagram": {
        "public_views": False,  # frequently withheld from anonymous requests
        "public_likes": True,
        "public_shares": False,  # no such public metric on Instagram
        "public_comment_count": True,
        "public_comment_text": False,  # auth-required and fragile
        "public_hashtags": True,
        "public_description": True,
        "private_retention_requires_manual_import": True,
        "demographics_requires_manual_import": True,
    },
    "manual": {
        "public_views": False,
        "public_likes": False,
        "public_shares": False,
        "public_comment_count": False,
        "public_comment_text": False,
        "public_hashtags": False,
        "public_description": False,
        "private_retention_requires_manual_import": True,
        "demographics_requires_manual_import": True,
    },
}

_UNKNOWN_CAPABILITIES: dict = {
    "public_views": None,
    "public_likes": None,
    "public_shares": None,
    "public_comment_count": None,
    "public_comment_text": None,
    "public_hashtags": None,
    "public_description": None,
    "private_retention_requires_manual_import": True,
    "demographics_requires_manual_import": True,
}


def capabilities_for(platform: str | None) -> dict:
    """Returns a shallow copy so the caller can safely embed it in a
    manifest without accidentally mutating the shared record."""
    if not platform:
        return {"platform": "unknown", **_UNKNOWN_CAPABILITIES}
    key = platform.lower()
    record = _CAPABILITIES.get(key, _UNKNOWN_CAPABILITIES)
    return {"platform": key, **record}
