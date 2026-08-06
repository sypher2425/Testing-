"""Subtitle (VTT/SRT) parsing, in two shapes.

`subtitles_to_text` produces reading/LLM-ingestion prose: timestamps, cue
indices and formatting tags are stripped, the duplicated rolling lines that
auto-captions produce (each cue repeats the previous line before adding the
new one) are collapsed, and the result is merged into paragraphs.

`subtitles_to_segments` keeps the cue timing, producing the same
{start, end, text} segment list that Whisper transcription produces — so a
platform-caption transcript and a Whisper transcript are interchangeable
downstream (same JSON, same SRT, same frame alignment).
"""
import html
import re

_TIMING_LINE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->")
_CUE_INDEX = re.compile(r"^\d+$")
_INLINE_TAG = re.compile(r"<[^>]*>")
_BRACKET_ONLY = re.compile(r"^[\[(][^\])]{0,60}[\])]$")
_SPEAKER_PREFIX = re.compile(r"^(?:>>+|-)\s+")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?$")

_HEADER_PREFIXES = ("WEBVTT", "Kind:", "Language:")
_BLOCK_PREFIXES = ("NOTE", "STYLE", "REGION")

# Cue timing, both dialects: "00:00:01.500 --> 00:00:03.000" (VTT, dot) and
# "00:00:01,500 --> 00:00:03,000" (SRT, comma). Hours are optional in VTT.
_CUE_TIMING = re.compile(
    r"(?P<start>\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3})"
)


def _cue_time_to_seconds(value: str) -> float:
    """"00:01:02.500" or "01:02.500" -> seconds. mm:ss is the VTT short form."""
    clock, _, fraction = value.replace(",", ".").partition(".")
    parts = [int(p) for p in clock.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        hours, (minutes, seconds) = 0, parts
    total = hours * 3600 + minutes * 60 + seconds
    # VTT allows 1-3 fraction digits; ".5" means 500ms, not 5ms.
    return total + (int(fraction) / (10 ** len(fraction)) if fraction else 0.0)


def subtitles_to_segments(content: str) -> list[dict]:
    """Parse VTT/SRT into timed segments: [{start, end, text}, ...].

    Same cleaning as subtitles_to_text (tags, speaker arrows, entities,
    caption-only bracket cues), but timing is kept — which is what makes a
    platform-caption transcript interchangeable with a Whisper one.

    Rolling auto-captions carry the previous cue's line into the next cue
    before appending the new one, so deduplication happens per *line*, not per
    cue — a whole-cue comparison would keep "A" then "A B". Only an
    immediately-preceding repeat is dropped (same rule as subtitles_to_text),
    which leaves a genuinely repeated phrase later in the video intact.
    A cue left with no lines contributes no segment.
    """
    segments: list[dict] = []
    pending: tuple[float, float] | None = None
    text_lines: list[str] = []
    last_norm = ""  # last emitted line, for the rolling-caption repeat

    def flush() -> None:
        nonlocal pending, text_lines
        if pending is not None and text_lines:
            text = " ".join(text_lines).strip()
            if text:
                start, end = pending
                segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        pending = None
        text_lines = []

    in_block = False
    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            flush()
            in_block = False
            continue
        if in_block:
            continue
        if line.startswith(_BLOCK_PREFIXES):
            in_block = True
            continue
        if line.startswith(_HEADER_PREFIXES):
            continue

        timing = _CUE_TIMING.search(line)
        if timing:
            flush()
            pending = (
                _cue_time_to_seconds(timing.group("start")),
                _cue_time_to_seconds(timing.group("end")),
            )
            continue
        if pending is None:
            # A cue index, or a stray line before any timing — nothing to
            # attach it to either way.
            continue

        text = _INLINE_TAG.sub("", line)
        text = html.unescape(text).replace("\u200b", " ")
        text = _SPEAKER_PREFIX.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or _BRACKET_ONLY.match(text):
            continue
        norm = text.casefold()
        if norm == last_norm:
            continue
        text_lines.append(text)
        last_norm = norm

    flush()
    return segments


def subtitles_to_text(content: str, *, paragraph_target_chars: int = 550) -> str:
    """Convert raw VTT or SRT content into clean paragraphs of plain text."""
    lines_out: list[str] = []
    last_norm = ""
    in_block = False  # inside a NOTE/STYLE/REGION block (ends at blank line)

    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            in_block = False
            continue
        if in_block:
            continue
        if line.startswith(_BLOCK_PREFIXES):
            in_block = True
            continue
        if line.startswith(_HEADER_PREFIXES):
            continue
        if _TIMING_LINE.search(line):
            continue
        if _CUE_INDEX.match(line):
            continue

        text = _INLINE_TAG.sub("", line)
        text = html.unescape(text).replace("\u200b", " ")
        text = _SPEAKER_PREFIX.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or _BRACKET_ONLY.match(text):
            continue

        # Collapse the rolling-caption duplicate pattern: auto-caption cues
        # repeat the previous cue's line before appending the new one.
        norm = text.casefold()
        if norm == last_norm:
            continue
        lines_out.append(text)
        last_norm = norm

    if not lines_out:
        return ""

    paragraphs: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for ln in lines_out:
        buf.append(ln)
        buf_len += len(ln) + 1
        # Prefer breaking at a sentence boundary once the paragraph is long
        # enough; auto captions often lack punctuation entirely, so also hard
        # break well past the target rather than emitting one giant blob.
        if buf_len >= paragraph_target_chars and (_SENTENCE_END.search(ln) or buf_len >= paragraph_target_chars * 3):
            paragraphs.append(" ".join(buf))
            buf = []
            buf_len = 0
    if buf:
        paragraphs.append(" ".join(buf))

    return "\n\n".join(paragraphs) + "\n"
