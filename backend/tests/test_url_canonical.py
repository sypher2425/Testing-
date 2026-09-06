from app.utils.url_canonical import canonicalize


def test_tiktok_full_url_with_tracking_params_canonicalizes():
    result = canonicalize(
        "https://www.tiktok.com/@azthixroblox/video/7659289588673924372?is_from_webapp=1&sender_device=pc"
    )
    assert result.platform == "tiktok"
    assert result.post_id == "7659289588673924372"
    assert result.canonical == "https://www.tiktok.com/@azthixroblox/video/7659289588673924372"
    assert result.original.startswith("https://www.tiktok.com/@azthixroblox/video/7659289588673924372?")


def test_tiktok_short_link_kept_but_marked():
    # Short-link hosts don't carry the id in the URL — canonicalization can
    # identify the platform but not a post ID without a network follow.
    result = canonicalize("https://vm.tiktok.com/ZMabc123X/")
    assert result.platform == "tiktok"
    assert result.post_id is None
    assert "vm.tiktok.com" in result.canonical


def test_instagram_reel_with_igsh_param_stripped():
    result = canonicalize("https://www.instagram.com/reel/DaPXdrNoJ6y/?igsh=MWVsa3dnZGhyd3dtMw==")
    assert result.platform == "instagram"
    assert result.post_id == "DaPXdrNoJ6y"
    assert result.canonical == "https://www.instagram.com/reel/DaPXdrNoJ6y/"


def test_instagram_reels_alias_normalized_to_reel():
    result = canonicalize("https://www.instagram.com/reels/DaPXdrNoJ6y/")
    assert result.canonical == "https://www.instagram.com/reel/DaPXdrNoJ6y/"


def test_youtube_short_form_url_normalized():
    result = canonicalize("https://youtu.be/dQw4w9WgXcQ?si=abc123")
    assert result.platform == "youtube"
    assert result.post_id == "dQw4w9WgXcQ"
    assert result.canonical == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_shorts_url_normalized():
    result = canonicalize("https://www.youtube.com/shorts/dQw4w9WgXcQ?feature=share")
    assert result.canonical == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.post_id == "dQw4w9WgXcQ"


def test_youtube_full_watch_url_normalized():
    result = canonicalize("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=WL")
    assert result.canonical == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_unknown_host_returns_original_untouched():
    original = "https://example.com/video/xyz?token=secret"
    result = canonicalize(original)
    assert result.platform == "unknown"
    assert result.canonical == original  # never destructive on unknown formats
    assert result.post_id is None


def test_empty_input_never_raises():
    result = canonicalize("")
    assert result.platform == "unknown"
    assert result.post_id is None
