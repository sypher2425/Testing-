"""Derives content/audio.json from the transcript — pacing analysis that
separates overall video pacing from active narration pacing (a 49s video
with 118 words is not the same as 49s of continuous narration if it
contains long silent render/wait periods).

Everything here is measured from data we actually have (transcript segments,
video duration, source metadata); anything unknowable is null + status.
"""
from app.utils import status as st

# A gap between spoken segments longer than this is considered a
# silence/render-wait period rather than a natural speech pause.
SILENCE_GAP_THRESHOLD_SECONDS = 1.5


def compute_audio_stats(
    transcript: dict | None,
    duration_seconds: float | None,
    music_info: dict | None,
) -> dict:
    """Builds the audio.json payload. transcript is the internal transcript
    dict ({skipped, segments: [{start, end, text}]}); music_info is
    {track, artist, added_in_app?} from source metadata when available."""
    segments = (transcript or {}).get("segments") or []
    transcription_skipped = bool((transcript or {}).get("skipped", True))

    has_voiceover = bool(segments)
    if transcription_skipped:
        skip_reason = "No audio track was found or transcription was skipped"
        result: dict = {
            "original_audio_present": field(None, st.NOT_AVAILABLE, reason=skip_reason),
            "voiceover_present": field(None, st.NOT_AVAILABLE, reason=skip_reason),
        }
    else:
        result = {
            "original_audio_present": field(True, st.SUCCESS),
            "voiceover_present": field(has_voiceover, st.SUCCESS),
        }

    if segments:
        word_count = sum(len((s.get("text") or "").split()) for s in segments)
        narration_seconds = sum(max(0.0, (s.get("end") or 0) - (s.get("start") or 0)) for s in segments)
        # Span = time from the first spoken word to the end of the last one;
        # NOT the same as active narration time when there are render gaps.
        voiceover_span = (segments[-1].get("end") or 0) - (segments[0].get("start") or 0)
        silence_within_span = max(0.0, voiceover_span - narration_seconds)

        overall_wpm = None
        if duration_seconds and duration_seconds > 0:
            overall_wpm = round(word_count / (duration_seconds / 60.0), 1)
        narration_wpm = round(word_count / (narration_seconds / 60.0), 1) if narration_seconds > 0 else None

        silence_periods = []
        previous_end = 0.0
        for seg in segments:
            start = seg.get("start") or 0.0
            if start - previous_end >= SILENCE_GAP_THRESHOLD_SECONDS:
                silence_periods.append({"start": round(previous_end, 2), "end": round(start, 2)})
            previous_end = max(previous_end, seg.get("end") or start)
        if duration_seconds and duration_seconds - previous_end >= SILENCE_GAP_THRESHOLD_SECONDS:
            silence_periods.append({"start": round(previous_end, 2), "end": round(duration_seconds, 2)})

        result.update(
            {
                # R1.5 field names — precise about what each one measures.
                "voiceover_span_seconds": field(round(voiceover_span, 2), st.SUCCESS),
                "active_narration_seconds": field(round(narration_seconds, 2), st.SUCCESS),
                "silence_or_render_wait_seconds": field(round(silence_within_span, 2), st.SUCCESS),
                "spoken_word_count": field(word_count, st.SUCCESS),
                "overall_video_wpm": field(overall_wpm, st.SUCCESS if overall_wpm is not None else st.NOT_AVAILABLE),
                "active_narration_wpm": field(
                    narration_wpm, st.SUCCESS if narration_wpm is not None else st.NOT_AVAILABLE
                ),
                "silence_periods": field(silence_periods, st.SUCCESS),
                # Deprecated alias kept for backward compat with the R1 shape.
                "voiceover_duration_seconds": field(round(voiceover_span, 2), st.SUCCESS),
                "overall_wpm": field(overall_wpm, st.SUCCESS if overall_wpm is not None else st.NOT_AVAILABLE),
            }
        )
    else:
        no_speech_reason = (
            "Transcription was skipped (no audio track)" if transcription_skipped else "No speech was detected"
        )
        for key in (
            "voiceover_span_seconds",
            "active_narration_seconds",
            "silence_or_render_wait_seconds",
            "spoken_word_count",
            "overall_video_wpm",
            "active_narration_wpm",
            "silence_periods",
            # Deprecated aliases: keep them non-null-shaped for R1 consumers.
            "voiceover_duration_seconds",
            "overall_wpm",
        ):
            result[key] = field(None, st.NOT_AVAILABLE, reason=no_speech_reason)

    music = music_info or {}
    if music.get("track"):
        result["music_title"] = field(music.get("track"), st.SUCCESS)
        result["music_source"] = field(music.get("artist"), st.SUCCESS if music.get("artist") else st.NOT_AVAILABLE)
    else:
        reason = "Source metadata did not include music information"
        result["music_title"] = field(None, st.MANUAL_REQUIRED, reason=reason)
        result["music_source"] = field(None, st.MANUAL_REQUIRED, reason=reason)
    result["music_added_in_app"] = field(None, st.MANUAL_REQUIRED, reason="Not exposed by platforms; enter manually")
    result["sound_effect_timestamps"] = field(None, st.MANUAL_REQUIRED, reason="Requires manual annotation")

    result["_deprecated"] = {
        "voiceover_duration_seconds": "Renamed to voiceover_span_seconds in schema v2.1 (same value; span from first to last spoken word)",
        "overall_wpm": "Renamed to overall_video_wpm in schema v2.1 (same value; words / full video duration)",
    }

    return result


def field(value, status: str, reason: str | None = None) -> dict:
    return st.field_result(value, status, source=st.SOURCE_AUTO if status == st.SUCCESS else None, reason=reason)
