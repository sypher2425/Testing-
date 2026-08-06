"""Environment probes for the extraction health endpoint."""
import shutil
import subprocess


def ffmpeg_available() -> dict:
    """ffmpeg/ffprobe presence and version. Both are required: ffprobe reads
    the source, ffmpeg extracts audio and frames."""
    result: dict = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            result[tool] = {"available": False}
            continue
        try:
            proc = subprocess.run(
                [tool, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
            )
            first = proc.stdout.decode(errors="replace").splitlines()[:1]
            version = first[0] if first else ""
        except (subprocess.TimeoutExpired, OSError):
            version = "unknown"
        result[tool] = {"available": True, "version": version}
    return result
