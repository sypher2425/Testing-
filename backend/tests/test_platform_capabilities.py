from app.utils.platform_capabilities import capabilities_for


def test_tiktok_capabilities_have_shares_but_not_comment_text():
    caps = capabilities_for("tiktok")
    assert caps["platform"] == "tiktok"
    assert caps["public_shares"] is True
    assert caps["public_comment_text"] is False
    assert caps["private_retention_requires_manual_import"] is True


def test_instagram_capabilities_reflect_real_extractor_limits():
    caps = capabilities_for("instagram")
    assert caps["public_shares"] is False  # no such public metric
    assert caps["public_comment_text"] is False  # auth-required in practice
    assert caps["public_views"] is False  # often withheld from anonymous requests


def test_youtube_capabilities_reflect_extractor_reality():
    caps = capabilities_for("youtube")
    assert caps["public_comment_text"] is True  # yt-dlp reliably extracts these
    assert caps["public_shares"] is False


def test_unknown_platform_gets_unknown_marker_never_raises():
    caps = capabilities_for(None)
    assert caps["platform"] == "unknown"
    caps = capabilities_for("some_new_platform")
    assert caps["platform"] == "some_new_platform"
    assert caps["public_views"] is None  # honest "we don't know"


def test_capabilities_are_isolated_copies():
    a = capabilities_for("tiktok")
    a["public_views"] = "mutated"
    b = capabilities_for("tiktok")
    assert b["public_views"] is True
