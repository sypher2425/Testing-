from app.utils.captions import subtitles_to_text

ROLLING_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.500
so today we're going to talk

00:00:02.500 --> 00:00:05.000
so today we're going to talk
about<00:00:03.000><c> content</c><c> strategy</c>

00:00:05.000 --> 00:00:07.500
about content strategy
and why it matters

00:00:07.500 --> 00:00:09.000
[Music]
"""

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:03,000
Hello everyone.

2
00:00:03,000 --> 00:00:05,000
<i>Welcome back</i> to the channel.

3
00:00:05,000 --> 00:00:07,000
>> Let's get started.
"""


def test_vtt_rolling_captions_deduped_and_tags_removed():
    text = subtitles_to_text(ROLLING_VTT)
    assert "-->" not in text
    assert "<c>" not in text and "<00:" not in text
    assert "WEBVTT" not in text and "Kind:" not in text
    assert "[Music]" not in text
    # Rolling duplicates collapsed: each phrase appears exactly once.
    assert text.count("so today we're going to talk") == 1
    assert text.count("about content strategy") == 1
    assert "and why it matters" in text


def test_srt_indices_and_formatting_stripped():
    text = subtitles_to_text(SRT_SAMPLE)
    assert "Hello everyone." in text
    assert "Welcome back to the channel." in text
    assert "Let's get started." in text
    assert "<i>" not in text
    assert ">>" not in text
    # Cue index digits must not leak in as standalone content.
    assert "\n1\n" not in text and not text.startswith("1")


def test_paragraph_breaks_on_long_content():
    sentence = "This is a sentence about a topic that keeps going for a while."
    vtt = "WEBVTT\n\n" + "\n\n".join(
        f"00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\n{sentence} (part {i})" for i in range(30)
    )
    text = subtitles_to_text(vtt)
    assert "\n\n" in text  # at least one paragraph break
    assert text.endswith("\n")


def test_empty_or_headers_only_returns_empty():
    assert subtitles_to_text("WEBVTT\nKind: captions\n") == ""
    assert subtitles_to_text("") == ""
