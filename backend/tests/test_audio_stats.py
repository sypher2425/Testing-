from app.utils.audio_stats import compute_audio_stats

# 49s video, 12 words spoken across two segments with a long render gap —
# overall pacing and active-narration pacing must differ.
TRANSCRIPT = {
    "skipped": False,
    "segments": [
        {"start": 0.0, "end": 3.0, "text": "watch what happens when we add these"},  # 7 words / 3s
        {"start": 40.0, "end": 45.0, "text": "and that is the final result"},  # 6 words / 5s (37s gap)
    ],
}


def test_wpm_separates_overall_from_active_narration():
    stats = compute_audio_stats(TRANSCRIPT, duration_seconds=49.0, music_info=None)

    assert stats["voiceover_present"]["value"] is True
    assert stats["spoken_word_count"]["value"] == 13
    # overall: 13 words over 49s ≈ 15.9 wpm; narration: 13 words over 8s ≈ 97.5 wpm
    assert stats["overall_video_wpm"]["value"] == round(13 / (49 / 60), 1)
    assert stats["active_narration_wpm"]["value"] == round(13 / (8 / 60), 1)
    assert stats["overall_video_wpm"]["value"] < stats["active_narration_wpm"]["value"]
    assert stats["active_narration_seconds"]["value"] == 8.0
    # R1.5: renamed span field + explicit silence-inside-span field
    assert stats["voiceover_span_seconds"]["value"] == 45.0  # 45.0 - 0.0
    assert stats["silence_or_render_wait_seconds"]["value"] == 37.0  # 45s span - 8s narration
    # Deprecated aliases carry the SAME values with a deprecation note.
    assert stats["voiceover_duration_seconds"]["value"] == stats["voiceover_span_seconds"]["value"]
    assert stats["overall_wpm"]["value"] == stats["overall_video_wpm"]["value"]
    assert "voiceover_duration_seconds" in stats["_deprecated"]


def test_silence_periods_capture_render_gaps():
    stats = compute_audio_stats(TRANSCRIPT, duration_seconds=49.0, music_info=None)
    periods = stats["silence_periods"]["value"]
    assert {"start": 3.0, "end": 40.0} in periods
    assert {"start": 45.0, "end": 49.0} in periods


def test_no_audio_track_reports_not_available_never_zero():
    stats = compute_audio_stats({"skipped": True, "segments": []}, duration_seconds=10.0, music_info=None)
    assert stats["spoken_word_count"]["value"] is None
    assert stats["spoken_word_count"]["status"] == "not_available"
    assert stats["overall_wpm"]["value"] is None
    assert stats["original_audio_present"]["value"] is None


def test_music_info_auto_vs_manual_required():
    with_music = compute_audio_stats(TRANSCRIPT, 49.0, {"track": "Song X", "artist": "Artist Y"})
    assert with_music["music_title"]["value"] == "Song X"
    assert with_music["music_title"]["status"] == "success"

    without = compute_audio_stats(TRANSCRIPT, 49.0, None)
    assert without["music_title"]["value"] is None
    assert without["music_title"]["status"] == "manual_required"
