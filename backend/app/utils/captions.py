"""Subtitle (VTT/SRT) → clean plain-text transcript conversion.

The output is optimized for reading and LLM ingestion, not subtitle
playback: timestamps, cue indices, and formatting tags are stripped; the
duplicated rolling lines that YouTube auto-captions produce (each cue
repeats the previous line before adding the new one) are collapsed; and the
result is merged into readable paragraphs.
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
