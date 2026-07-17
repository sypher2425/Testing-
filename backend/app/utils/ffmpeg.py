"""Safe subprocess wrappers around ffmpeg/ffprobe.

All invocations pass argument lists (never shell=True, never string
interpolation) and are wrapped with a timeout and captured stderr so a
hung or failing process always turns into a typed, user-readable error
instead of a silent hang.
"""
import json
import os
import subprocess
from dataclasses import dataclass


class FFmpegError(Exception):
    def __init__(self, message: str, *, cmd: list[str], returncode: int | None, stderr: str):
        super().__init__(message)
        self.message = message
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr

    def to_detail(self) -> dict:
        return {
            "cmd": self.cmd,
            "returncode": self.returncode,
            "stderr": self.stderr[-4000:] if self.stderr else "",
        }


@dataclass
class ProbeResult:
    duration_seconds: float
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    has_audio: bool
    raw: dict


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            f"Command timed out after {timeout}s: {cmd[0]}",
            cmd=cmd,
            returncode=None,
            stderr=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"Executable not found: {cmd[0]}", cmd=cmd, returncode=None, stderr=str(exc)
        ) from exc


def ffprobe(path: str, timeout: int = 60) -> ProbeResult:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = _run(cmd, timeout)
    if proc.returncode != 0:
        raise FFmpegError(
            "ffprobe failed to read the video file; it may be corrupt or an unsupported container.",
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
    data = json.loads(proc.stdout.decode(errors="replace"))
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise FFmpegError(
            "No video stream found in the uploaded file.",
            cmd=cmd,
            returncode=proc.returncode,
            stderr="",
        )

    duration = float(data.get("format", {}).get("duration") or video_stream.get("duration") or 0.0)
    fps = None
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if rate and rate != "0/0":
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den) if den and float(den) != 0 else float(num)
        except (ValueError, ZeroDivisionError):
            fps = None

    return ProbeResult(
        duration_seconds=duration,
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        fps=fps,
        codec=video_stream.get("codec_name"),
        has_audio=audio_stream is not None,
        raw=data,
    )


def extract_frame_at(
    source_path: str,
    output_path: str,
    timestamp_seconds: float,
    *,
    max_dim: int | None = None,
    quality: int = 85,
    fmt: str = "jpeg",
    timeout: int = 60,
    accurate: bool = False,
) -> None:
    """Extract a single frame at timestamp_seconds, optionally downscaled to max_dim
    on the long edge, preserving aspect ratio.

    By default uses fast (keyframe-based) seeking with -ss before -i. Pass
    accurate=True to instead seek after -i: slower (decodes from the start),
    but frame-accurate — a useful fallback when fast seeking lands on a
    timestamp ffmpeg can't produce a frame for (e.g. right at EOF or in a
    file with sparse keyframes)."""
    vf_parts = []
    if max_dim:
        vf_parts.append(
            f"scale='if(gt(iw,ih),min(iw,{max_dim}),-2)':'if(gt(iw,ih),-2,min(ih,{max_dim}))'"
        )
    timestamp_arg = f"{max(timestamp_seconds, 0.0):.3f}"
    if accurate:
        cmd = ["ffmpeg", "-y", "-i", source_path, "-ss", timestamp_arg]
    else:
        cmd = ["ffmpeg", "-y", "-ss", timestamp_arg, "-i", source_path]
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += ["-frames:v", "1"]
    if fmt == "jpeg":
        cmd += ["-q:v", str(_jpeg_quality_to_ffmpeg_q(quality))]
    cmd += [output_path]
    proc = _run(cmd, timeout)
    if proc.returncode != 0:
        raise FFmpegError(
            f"Failed to extract frame at {timestamp_seconds:.3f}s",
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
    # ffmpeg can exit 0 while writing nothing if the seek timestamp lands at
    # or past the last decodable frame — verify a real file actually landed.
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise FFmpegError(
            f"ffmpeg exited successfully but produced no frame at {timestamp_seconds:.3f}s "
            "(likely sought past the last decodable frame)",
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )


def _jpeg_quality_to_ffmpeg_q(quality_0_100: int) -> int:
    """ffmpeg's -q:v for mjpeg is 2 (best) .. 31 (worst); map from a 0-100 quality scale."""
    quality_0_100 = max(1, min(100, quality_0_100))
    return round(31 - (quality_0_100 / 100) * 29)


def extract_audio_wav(source_path: str, output_path: str, timeout: int = 600) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        output_path,
    ]
    proc = _run(cmd, timeout)
    if proc.returncode != 0:
        raise FFmpegError(
            "Failed to extract audio track for transcription",
            cmd=cmd,
            returncode=proc.returncode,
            stderr=proc.stderr.decode(errors="replace"),
        )
