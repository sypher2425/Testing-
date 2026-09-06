"""Bounded FFmpeg decoding with original-timeline presentation timestamps.

FFmpeg supplies the codec support (including AV1); OpenCV is only needed by
the optional image novelty scorer, never to open the source video. Dense
sampling decodes once rather than starting a process for every JPEG.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.utils.ffmpeg import FFmpegError, _jpeg_quality_to_ffmpeg_q


_PTS = re.compile(r"\bn:\s*\d+\s+pts:\s*([-\d]+)\s+pts_time:([-+\d.eE]+)")
_TIME_BASE = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_NVIDIA_DECODERS = {
    "av1": "av1_cuvid", "h264": "h264_cuvid", "hevc": "hevc_cuvid",
    "vp9": "vp9_cuvid", "vp8": "vp8_cuvid", "mpeg2video": "mpeg2_cuvid",
}
# A missing driver or unsupported stream must not incur a failed GPU launch
# for every requested JPEG. Retry availability after a bounded cooldown.
_HARDWARE_FAILURE_UNTIL: dict[str, float] = {}


@lru_cache(maxsize=1)
def _available_nvidia_decoders() -> frozenset[str]:
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"],
                                capture_output=True, timeout=5, check=False)
        if result.returncode:
            return frozenset()
        names = set(re.findall(r"\b\w+_cuvid\b", result.stdout.decode(errors="replace")))
        return frozenset(names)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()


@lru_cache(maxsize=128)
def _source_codec(path: str, size: int, mtime_ns: int) -> str | None:
    # File identity keeps the cache valid when a path is reused for an upload.
    from app.utils.ffmpeg import ffprobe
    try:
        return ffprobe(path, timeout=5).codec
    except FFmpegError:
        return None


def _decoder_options(source_path: str, hwaccel: str, source_codec: str | None) -> tuple[list[str], str]:
    if not hwaccel or hwaccel == "none":
        return [], "software_default"
    if hwaccel not in {"auto", "cuda"}:
        return ["-hwaccel", hwaccel], f"hwaccel_requested:{hwaccel}"
    if not source_codec:
        try:
            stat = Path(source_path).stat()
            source_codec = _source_codec(source_path, stat.st_size, stat.st_mtime_ns)
        except OSError:
            return [], "software_default"
    decoder = _NVIDIA_DECODERS.get(source_codec)
    if (decoder and decoder in _available_nvidia_decoders()
            and _HARDWARE_FAILURE_UNTIL.get(decoder, 0) <= time.monotonic()):
        # Explicit CUVID is essential for AV1: '-hwaccel auto' may silently
        # select libdav1d software decoding even on an AV1-capable NVIDIA GPU.
        return ["-c:v", decoder], decoder
    return [], "software_default"


def run_decode_process(
    cmd: list[str], *, source_path: str, hwaccel: str, source_codec: str | None,
    collect: "TimestampCollector", clear_outputs: Callable[[], None], timeout: float,
    check_cancel: Callable[[], None] | None, heartbeat: Callable[[], None] | None,
    decode_stats: dict | None = None, keyframes_only: bool = False,
) -> str:
    """Try one explicit hardware decoder, then CPU once on FFmpeg failure.

    Cancellation exceptions propagate unchanged. The retry shares the same
    timeout budget and discards partial hardware images and timestamps.
    """
    started = time.monotonic()
    if check_cancel:
        check_cancel()
    if keyframes_only:
        # CUVID ignores skip_frame=nokey and decodes every source frame. Use
        # software here so Fast prescans both honor keyframe-only semantics
        # and avoid paying for a complete high-resolution decode.
        options, decoder = [], "software_default"
        if decode_stats is not None and hwaccel and hwaccel != "none":
            bypasses = decode_stats.setdefault("hardware_bypasses", {})
            bypasses["keyframes_only"] = bypasses.get("keyframes_only", 0) + 1
    else:
        options, decoder = _decoder_options(source_path, hwaccel, source_codec)
    input_index = cmd.index("-i")
    accelerated = cmd[:input_index] + options + cmd[input_index:]
    try:
        stderr = run_media_process(
            accelerated, timeout=max(0.01, timeout - (time.monotonic() - started)),
            check_cancel=check_cancel, heartbeat=heartbeat, on_line=collect,
        )
    except FFmpegError as exc:
        if not options:
            raise
        if check_cancel:
            check_cancel()
        if decoder in _NVIDIA_DECODERS.values():
            _HARDWARE_FAILURE_UNTIL[decoder] = time.monotonic() + 60
        if decode_stats is not None:
            decode_stats["hardware_fallbacks"] = decode_stats.get("hardware_fallbacks", 0) + 1
            decode_stats["last_failed_decoder"] = decoder
            decode_stats["last_fallback_reason"] = exc.message
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise
        clear_outputs()
        collect.timestamps.clear()
        collect.time_base = None
        stderr = run_media_process(cmd, timeout=remaining, check_cancel=check_cancel,
                                   heartbeat=heartbeat, on_line=collect)
        decoder = "software_default"
    if decode_stats is not None:
        decode_stats["requested_hwaccel"] = hwaccel
        counts = decode_stats.setdefault("successful_decode_operations", {})
        counts[decoder] = counts.get(decoder, 0) + 1
    return stderr


class TimestampCollector:
    """Use integer PTS/timebase: showinfo's pts_time rounds long timestamps."""
    def __init__(self, limit: int):
        self.limit = limit
        self.timestamps: list[float] = []
        self.time_base: tuple[int, int] | None = None

    def __call__(self, line: str) -> None:
        base = _TIME_BASE.search(line)
        if base:
            self.time_base = (int(base.group(1)), int(base.group(2)))
        match = _PTS.search(line)
        if match and len(self.timestamps) < self.limit:
            value = (int(match.group(1)) * self.time_base[0] / self.time_base[1]
                     if self.time_base and self.time_base[1] else float(match.group(2)))
            self.timestamps.append(value)


@dataclass(frozen=True)
class DecodedFrame:
    path: Path
    timestamp: float


def run_media_process(
    cmd: list[str], *, timeout: float, check_cancel: Callable[[], None] | None = None,
    heartbeat: Callable[[], None] | None = None, on_line: Callable[[str], None] | None = None,
) -> str:
    """Drain stderr continuously, poll cancellation, and always reap the child.

Only a bounded diagnostic tail is retained. Callbacks that can touch the
database run on the caller's thread. The reader only parses media metadata.
"""
    tail: deque[str] = deque(maxlen=120)
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except (FileNotFoundError, OSError) as exc:
        raise FFmpegError(str(exc), cmd=cmd, returncode=None, stderr=str(exc)) from exc

    def drain() -> None:
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            tail.append(line)
            if on_line:
                on_line(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    started = time.monotonic()
    next_tick = started
    next_heartbeat = started
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now >= next_tick:
                if check_cancel:
                    check_cancel()
                next_tick = now + 0.5
            if heartbeat and now >= next_heartbeat:
                heartbeat()
                next_heartbeat = now + 5.0
            if now - started >= timeout:
                raise FFmpegError(
                    f"Frame decoding timed out after {timeout:g}s",
                    cmd=cmd, returncode=None, stderr="".join(tail),
                )
            time.sleep(0.05)
        reader.join(timeout=3)
        if check_cancel:
            check_cancel()
        stderr = "".join(tail)
        if process.returncode:
            raise FFmpegError(
                "FFmpeg could not decode the selected video range",
                cmd=cmd, returncode=process.returncode, stderr=stderr,
            )
        return stderr
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        reader.join(timeout=3)
        if process.stderr:
            process.stderr.close()


def decode_frames(
    source_path: str, output_dir: str | Path, *, start: float, end: float,
    max_frames: int, interval: float | None = None, timestamps: list[float] | None = None,
    max_dim: int = 1280, quality: int = 85, fmt: str = "jpeg", timeout: float = 600,
    check_cancel: Callable[[], None] | None = None, heartbeat: Callable[[], None] | None = None,
    keyframes_only: bool = False, hwaccel: str = "auto", source_codec: str | None = None,
    decode_stats: dict | None = None,
) -> list[DecodedFrame]:
    """Extract unique source frames, retaining their PTS instead of inventing FPS.

With neither interval nor timestamps this is a true every-decoded-frame
operation. An interval selects the first available source frame at or after
each grid point; it never duplicates or interpolates frames. ``max_frames``
is a hard output guard (call with budget + 1 to detect exhaustive overflow).
"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if max_frames < 1 or end <= start:
        return []
    ext = "jpg" if fmt == "jpeg" else "png"
    pattern = str(output_dir / f"decoded-%08d.{ext}")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "info"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    # start_at_zero removes a container start offset; copyts preserves the
    # position in the original video even when input seeking skips its prefix.
    cmd += ["-copyts", "-start_at_zero", "-ss", f"{start:.9f}",
            "-t", f"{end - start:.9f}", "-i", source_path, "-map", "0:v:0", "-an", "-sn"]
    filters = [f"trim=start={start:.9f}:end={end:.9f}"]
    if timestamps:
        ordered = sorted(set(t for t in timestamps if start <= t < end))
        if not ordered:
            return []
        # A batch is intentionally bounded by the caller, keeping filter
        # parsing/evaluation small even for 20,000-frame plans.
        terms = [f"gte(t,{t:.9f})*lt(prev_selected_t,{t:.9f})" for t in ordered]
        # FFmpeg comparisons involving NaN return false, but NaN arithmetic
        # can poison a sum. Explicitly branch for the first selected frame.
        expression = f"if(isnan(prev_selected_t),gte(t,{ordered[0]:.9f})," + "+".join(terms) + ")"
        filters.append(f"select='{expression}'")
    elif interval is not None:
        interval = max(float(interval), 0.000001)
        # The grid follows time, not selected_n: requests faster than the
        # source FPS must not build up a backlog and later oversample a VFR gap.
        expression = (
            f"if(isnan(prev_selected_t),gte(t,{start:.9f}),"
            f"gte(t+0.00000001,{start:.9f}+(floor((prev_selected_t-{start:.9f}+0.00000001)/{interval:.9f})+1)*{interval:.9f}))"
        )
        filters.append(f"select='{expression}'")
    if max_dim:
        filters.append(
            f"scale='if(gt(iw,ih),min(iw,{max_dim}),-2)':'if(gt(iw,ih),-2,min(ih,{max_dim}))'"
        )
    filters.append("showinfo")
    cmd += ["-vf", ",".join(filters), "-fps_mode", "vfr", "-frames:v", str(max_frames), "-threads", "2"]
    if fmt == "jpeg":
        cmd += ["-q:v", str(_jpeg_quality_to_ffmpeg_q(quality))]
    cmd += [pattern]
    collect = TimestampCollector(max_frames + 16)
    def clear_outputs() -> None:
        for path in output_dir.glob(f"decoded-*.{ext}"):
            path.unlink()
    run_decode_process(
        cmd, source_path=source_path, hwaccel=hwaccel, source_codec=source_codec,
        collect=collect, clear_outputs=clear_outputs, timeout=timeout,
        check_cancel=check_cancel, heartbeat=heartbeat, decode_stats=decode_stats,
        keyframes_only=keyframes_only,
    )
    pts = collect.timestamps
    paths = sorted(output_dir.glob(f"decoded-*.{ext}"))
    if len(paths) > len(pts):
        raise FFmpegError("Decoded images did not have matching source timestamps", cmd=cmd, returncode=0, stderr="")
    return [DecodedFrame(path, pts[i]) for i, path in enumerate(paths) if path.stat().st_size > 0]
