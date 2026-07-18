"""Subprocess wrapper around the yt-dlp CLI for URL ingestion.

Deliberately shells out to the `yt-dlp` binary rather than importing yt-dlp
as a Python library: when an extraction looks like a broken/outdated
extractor, we self-update via `pip install --upgrade yt-dlp` and retry once.
Since yt-dlp is invoked as a fresh subprocess each time, that retry picks up
the newly installed version automatically — no module-reload gymnastics
needed. The pinned version in requirements.txt is what every fresh container
starts with; self-update only ever happens on-demand, in response to a real
failure, and never on startup.
"""
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.config import get_settings

# Substrings that indicate the *video itself* is unavailable — updating
# yt-dlp will not help here, so these are never eligible for the
# self-update-and-retry path.
_UNAVAILABLE_MARKERS = [
    "private video",
    "video unavailable",
    "video has been removed",
    "this video is no longer available",
    "account is private",
    "sign in to confirm your age",
    "requires payment",
    "content isn't available",
    "content is not available",
    "geo restricted",
    "not available in your country",
    "copyright",
    "removed by the uploader",
    "this video has been removed",
]

# Substrings that indicate yt-dlp's extractor itself is broken/outdated for
# this site (the site changed something yt-dlp doesn't know how to parse
# yet) — worth a self-update-and-retry.
_EXTRACTOR_ERROR_MARKERS = [
    "unable to extract",
    "unsupported url",
    "no extractor found",
    "unable to download webpage",
    "unable to download api page",
    "did not get any data blocks",
    "please report this issue",
]


class YtDlpError(Exception):
    def __init__(self, code: str, message: str, *, stderr: str = ""):
        super().__init__(message)
        self.code = code  # "video_unavailable" | "extractor_outdated" | "download_failed"
        self.message = message
        self.stderr = stderr

    def to_detail(self) -> dict:
        return {"code": self.code, "stderr": self.stderr[-4000:] if self.stderr else ""}


@dataclass
class VideoMetadata:
    source_url: str
    platform: str
    title: str | None
    description: str | None
    uploader: str | None
    upload_date: str | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None
    hashtags: list[str]
    duration: float | None
    ext: str | None
    filesize_approx: int | None
    raw: dict = field(repr=False, default_factory=dict)


def _classify(stderr: str) -> str:
    lowered = stderr.lower()
    if any(marker in lowered for marker in _UNAVAILABLE_MARKERS):
        return "video_unavailable"
    if any(marker in lowered for marker in _EXTRACTOR_ERROR_MARKERS):
        return "extractor_outdated"
    return "download_failed"


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    cookies_file = get_settings().COOKIES_FILE
    cmd = ["yt-dlp"]
    cookies_scratch_path: str | None = None
    # COOKIES_FILE is allowed to point at a file that doesn't exist yet
    # (e.g. the user hasn't dropped one into secrets/ yet) — degrade to no
    # cookies rather than making every single fetch fail on a missing file.
    if cookies_file and Path(cookies_file).is_file():
        # yt-dlp writes updated cookies back to this path when it's done
        # (to persist a refreshed session) — the file is mounted read-only
        # by design (see secrets/README.md), so hand yt-dlp a scratch copy
        # instead of the original. Otherwise every invocation using cookies
        # crashes with "Read-only file system" on exit.
        scratch = tempfile.NamedTemporaryFile(delete=False, suffix=".cookies.txt")
        scratch.close()
        shutil.copyfile(cookies_file, scratch.name)
        cookies_scratch_path = scratch.name
        cmd += ["--cookies", cookies_scratch_path]
    cmd += args
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise YtDlpError(
            "download_failed", f"yt-dlp timed out after {timeout}s", stderr=str(exc)
        ) from exc
    except FileNotFoundError as exc:
        raise YtDlpError("download_failed", "yt-dlp is not installed", stderr=str(exc)) from exc
    finally:
        if cookies_scratch_path:
            Path(cookies_scratch_path).unlink(missing_ok=True)


def get_version() -> str:
    proc = _run(["--version"], timeout=30)
    return proc.stdout.decode(errors="replace").strip()


def _self_update(log: callable) -> None:
    """Attempt exactly one `pip install --upgrade yt-dlp`. Never raises —
    an update failure just means we retry with whatever's already
    installed (the pinned version), which is the correct fallback."""
    old_version = get_version()
    log("warning", f"yt-dlp extraction looked like a broken/outdated extractor (current version {old_version}); attempting a one-time self-update")
    settings = get_settings()
    try:
        proc = subprocess.run(
            ["pip", "install", "--upgrade", "yt-dlp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=settings.YTDLP_UPDATE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            log("warning", f"yt-dlp self-update failed, continuing with pinned version {old_version}")
            return
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log("warning", f"yt-dlp self-update failed ({exc}), continuing with pinned version {old_version}")
        return

    new_version = get_version()
    if new_version == old_version:
        log("info", f"yt-dlp self-update ran but version is unchanged ({old_version}) — already current")
    else:
        log("info", f"yt-dlp self-updated: {old_version} -> {new_version}")


def _run_with_extractor_retry(args: list[str], timeout: int, log: callable) -> subprocess.CompletedProcess:
    proc = _run(args, timeout)
    if proc.returncode == 0:
        return proc

    stderr = proc.stderr.decode(errors="replace")
    code = _classify(stderr)
    if code != "extractor_outdated":
        raise YtDlpError(code, _first_error_line(stderr) or "yt-dlp failed", stderr=stderr)

    # Looks like a broken/outdated extractor, not a 404/private video: self-update once, retry once.
    _self_update(log)
    retry_proc = _run(args, timeout)
    if retry_proc.returncode == 0:
        return retry_proc

    retry_stderr = retry_proc.stderr.decode(errors="replace")
    retry_code = _classify(retry_stderr)
    raise YtDlpError(retry_code, _first_error_line(retry_stderr) or "yt-dlp failed after self-update retry", stderr=retry_stderr)


def _first_error_line(stderr: str) -> str | None:
    for line in stderr.splitlines():
        if line.strip().startswith("ERROR"):
            return line.strip()
    return None


_HASHTAG_RE = re.compile(r"#(\w+)")


def _extract_hashtags(info: dict) -> list[str]:
    tags = [t for t in (info.get("tags") or []) if isinstance(t, str)]
    description = info.get("description") or ""
    from_description = _HASHTAG_RE.findall(description)
    seen: list[str] = []
    for tag in [*tags, *from_description]:
        normalized = tag.lstrip("#")
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen[:50]


def _platform_from_extractor(info: dict) -> str:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if "youtube" in extractor:
        return "youtube"
    if "tiktok" in extractor:
        return "tiktok"
    if "instagram" in extractor:
        return "instagram"
    return extractor or "unknown"


def _upload_date_iso(info: dict) -> str | None:
    raw = info.get("upload_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _cookies_status() -> str:
    cookies_file = get_settings().COOKIES_FILE
    if not cookies_file:
        return "not configured"
    return "in use" if Path(cookies_file).is_file() else f"configured ({cookies_file}) but file not found"


def extract_metadata(url: str, *, log: callable) -> VideoMetadata:
    settings = get_settings()
    log("info", f"yt-dlp cookies: {_cookies_status()}")
    proc = _run_with_extractor_retry(
        ["--dump-single-json", "--skip-download", "--no-warnings", url],
        timeout=settings.YTDLP_METADATA_TIMEOUT_SECONDS,
        log=log,
    )
    info = json.loads(proc.stdout.decode(errors="replace"))
    return VideoMetadata(
        source_url=url,
        platform=_platform_from_extractor(info),
        title=info.get("title"),
        description=info.get("description"),
        uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        upload_date=_upload_date_iso(info),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        comment_count=info.get("comment_count"),
        share_count=info.get("repost_count"),
        hashtags=_extract_hashtags(info),
        duration=info.get("duration"),
        ext=info.get("ext"),
        filesize_approx=info.get("filesize") or info.get("filesize_approx"),
        raw=info,
    )


def download_video(url: str, dest_dir: Path, *, log: callable) -> Path:
    """Downloads the video into dest_dir, named 'video.<ext>'. Returns the
    resulting path."""
    settings = get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(dest_dir / "video.%(ext)s")
    _run_with_extractor_retry(
        [
            "--no-warnings",
            "--no-playlist",
            "-f",
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format",
            "mp4",
            "-o",
            output_template,
            url,
        ],
        timeout=settings.YTDLP_TIMEOUT_SECONDS,
        log=log,
    )
    matches = sorted(dest_dir.glob("video.*"))
    if not matches:
        raise YtDlpError("download_failed", "yt-dlp reported success but no output file was found")
    return matches[0]


def extract_comments(url: str, *, limit: int, log: callable) -> list[dict]:
    """Best-effort: never raises. Returns [] (and logs a warning) on any
    failure — comment extraction is a bonus, not required for the job to
    succeed, and is known to be fragile on TikTok/Instagram."""
    settings = get_settings()
    try:
        proc = _run(
            ["--dump-single-json", "--skip-download", "--write-comments", "--no-warnings", url],
            timeout=settings.YTDLP_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            log("warning", f"Comment extraction failed, continuing without comments: {_first_error_line(proc.stderr.decode(errors='replace'))}")
            return []
        info = json.loads(proc.stdout.decode(errors="replace"))
        comments = info.get("comments") or []
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        log("warning", f"Comment extraction failed, continuing without comments: {exc}")
        return []

    def _likes(c: dict) -> int:
        return c.get("like_count") or 0

    top = sorted(comments, key=_likes, reverse=True)[:limit]
    return [
        {
            "author": c.get("author"),
            "text": c.get("text"),
            "like_count": c.get("like_count"),
            "timestamp": c.get("timestamp"),
        }
        for c in top
    ]
