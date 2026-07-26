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


def test_silence_analysis_scopes_and_totals_are_explicit():
    """v2.2: the span-scoped total and the whole-video listed gaps are
    different measurements and must be labeled as such."""
    from app.utils.audio_stats import SILENCE_GAP_THRESHOLD_SECONDS, compute_audio_stats

    transcript = {
        "skipped": False,
        "segments": [
            {"start": 2.0, "end": 4.0, "text": "hello there"},
            {"start": 9.0, "end": 11.0, "text": "welcome back"},  # 5s gap in span
        ],
    }
    stats = compute_audio_stats(transcript, duration_seconds=20.0, music_info=None)
    analysis = stats["silence_analysis"]

    # Total: gaps inside the span only (9-4=5s), regardless of threshold.
    assert analysis["total_non_narration_seconds"] == 5.0
    assert analysis["total_scope"] == "all_gaps_within_voiceover_span"

    # Listed: whole-video gaps >= threshold, incl. lead-in (0-2) and tail (11-20).
    assert analysis["listed_periods_minimum_duration_seconds"] == SILENCE_GAP_THRESHOLD_SECONDS
    assert analysis["listed_periods_scope"] == "full_video_gaps_over_threshold"
    listed = analysis["listed_periods"]
    assert [(p["start"], p["end"]) for p in listed] == [(0.0, 2.0), (4.0, 9.0), (11.0, 20.0)]
    for p in listed:
        assert p["duration_seconds"] == round(p["end"] - p["start"], 2)
    assert analysis["listed_periods_total_seconds"] == round(sum(p["duration_seconds"] for p in listed), 2)


def test_silence_deprecated_aliases_mirror_canonical_values():
    from app.utils.audio_stats import compute_audio_stats

    transcript = {
        "skipped": False,
        "segments": [
            {"start": 0.0, "end": 3.0, "text": "one two three"},
            {"start": 8.0, "end": 10.0, "text": "four five"},
        ],
    }
    stats = compute_audio_stats(transcript, duration_seconds=10.0, music_info=None)
    assert (
        stats["silence_or_render_wait_seconds"]["value"]
        == stats["silence_analysis"]["total_non_narration_seconds"]
    )
    legacy = stats["silence_periods"]["value"]
    listed = stats["silence_analysis"]["listed_periods"]
    assert [(p["start"], p["end"]) for p in listed] == [(p["start"], p["end"]) for p in legacy]
    assert "silence_or_render_wait_seconds" in stats["_deprecated"]
    assert "silence_periods" in stats["_deprecated"]


def test_silence_analysis_not_available_shape_when_no_speech():
    from app.utils.audio_stats import compute_audio_stats

    stats = compute_audio_stats({"skipped": True, "segments": []}, duration_seconds=10.0, music_info=None)
    analysis = stats["silence_analysis"]
    assert analysis["status"] == "not_available"
    assert analysis["total_non_narration_seconds"] is None
    assert analysis["listed_periods"] == []
    assert analysis["listed_periods_total_seconds"] is None


def test_computed_source_and_entry_method_on_success_values():
    """v2.2: audio stats are derived from the transcript — provenance is
    'computed', never the deprecated 'auto'."""
    from app.utils.audio_stats import compute_audio_stats

    transcript = {"skipped": False, "segments": [{"start": 0.0, "end": 2.0, "text": "hi there"}]}
    stats = compute_audio_stats(transcript, duration_seconds=4.0, music_info=None)
    record = stats["spoken_word_count"]
    assert record["source"] == "computed"
    assert record["entry_method"] == "automatic"
