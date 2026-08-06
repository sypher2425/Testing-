"""ffprobe/ffmpeg wrapper behaviour that the rest of the pipeline relies on."""
import pytest

from app.utils.ffmpeg import FFmpegError, ffprobe

def test_ffprobe_accepts_an_audio_only_file_when_video_is_not_required(tmp_path):
    """Transcript mode feeds .mp3/.m4a through this — there are no frames to
    extract, so a missing video stream is not a defect."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not available")

    audio = tmp_path / "tone.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )

    # The default still rejects it: the dataset pipeline genuinely needs video.
    with pytest.raises(FFmpegError) as exc_info:
        ffprobe(str(audio))
    assert "No video stream" in exc_info.value.message

    probe = ffprobe(str(audio), require_video=False)
    assert probe.has_audio is True
    assert probe.duration_seconds > 1.5
    assert probe.codec == "aac"
    # Honest nulls rather than invented dimensions.
    assert probe.width is None and probe.height is None and probe.fps is None
