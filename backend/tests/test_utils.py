from app.utils.filenames import frame_filename, is_safe_relative_path, sanitize_filename


def test_sanitize_filename_strips_path_and_unsafe_chars():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("my video (final)!!.mp4") == "my_video_final_.mp4"
    assert sanitize_filename("") == "upload"


def test_frame_filename_zero_padded_with_millis():
    assert frame_filename(3.5) == "0003.500.jpg"
    assert frame_filename(0.0) == "0000.000.jpg"
    assert frame_filename(125.999, fmt="png") == "0125.999.png"


def test_frame_filename_rounds_millis_overflow():
    # 0.9995 rounds to 1000ms, which must roll into the next whole second.
    assert frame_filename(0.9996) == "0001.000.jpg"


def test_is_safe_relative_path_rejects_traversal():
    assert is_safe_relative_path("frames/0001.000.jpg") is True
    assert is_safe_relative_path("../secret") is False
    assert is_safe_relative_path("/etc/passwd") is False
    assert is_safe_relative_path("a/../../b") is False
