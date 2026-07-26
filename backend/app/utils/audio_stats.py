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
        voiceover_span = (segments[-1].get("end") or 0) - (segments[0].get("start") or 0)

        overall_wpm = None
        if duration_seconds and duration_seconds > 0:
            overall_wpm = round(word_count / (duration_seconds / 60.0))
        narration_wpm = round(word_count / (narration_seconds / 60.0)) if narration_seconds > 0 else None

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
                "voiceover_duration_seconds": field(round(voiceover_span, 2), st.SUCCESS),
                "spoken_word_count": field(word_count, st.SUCCESS),
                "overall_wpm": field(overall_wpm, st.SUCCESS if overall_wpm is not None else st.NOT_AVAILABLE),
                "active_narration_seconds": field(round(narration_seconds, 2), st.SUCCESS),
                "active_narration_wpm": field(
                    narration_wpm, st.SUCCESS if narration_wpm is not None else st.NOT_AVAILABLE
                ),
                "silence_periods": field(silence_periods, st.SUCCESS),
            }
        )
    else:
        no_speech_reason = (
            "Transcription was skipped (no audio track)" if transcription_skipped else "No speech was detected"
        )
        for key in (
            "voiceover_duration_seconds",
            "spoken_word_count",
            "overall_wpm",
            "active_narration_seconds",
            "active_narration_wpm",
            "silence_periods",
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

    return result


def field(value, status: str, reason: str | None = None) -> dict:
    return st.field_result(value, status, source=st.SOURCE_AUTO if status == st.SUCCESS else None, reason=reason)
