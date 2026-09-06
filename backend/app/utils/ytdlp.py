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
import time
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.utils.extraction_errors import (
    RETRY_ANONYMOUSLY,
    Classification,
    classify,
    reconcile,
)
from app.utils.timestamps import now_utc_iso

class YtDlpError(Exception):
    def __init__(self, code: str, message: str, *, stderr: str = ""):
        proxy = get_settings().YTDLP_PROXY
        message = _redact_proxy_output(message, proxy)
        super().__init__(message)
        self.code = code  # "video_unavailable" | "extractor_outdated" | "download_failed"
        self.message = message
        self.stderr = _redact_proxy_output(stderr, proxy)

    def to_detail(self) -> dict:
        return {"code": self.code, "stderr": self.stderr[-4000:] if self.stderr else ""}


@dataclass
class VideoMetadata:
    source_url: str
    platform: str
    title: str | None
    description: str | None
    uploader: str | None
    uploader_id: str | None
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


def redact_proxy(value: str) -> str:
    """Proxy URLs routinely carry credentials. Everything that surfaces the
    proxy — logs, the diagnostics endpoint, error detail — goes through this,
    so a password never leaves the process."""
    if not value:
        return ""
    match = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<authority>.+)$", value)
    if not match:
        return "(configured)"
    # Split at the last @ so a password containing @ cannot become part of
    # the reported host. The proxy is never useful with its userinfo attached.
    authority = match.group("authority")
    return f"{match.group('scheme')}***@{authority.rsplit('@', 1)[-1]}" if "@" in authority else value


def _redact_proxy_output(value: str, proxy: str) -> str:
    """Scrub an echoed configured proxy before diagnostics leave this module."""
    return value.replace(proxy, redact_proxy(proxy)) if proxy else value


def proxy_status() -> str:
    proxy = get_settings().YTDLP_PROXY
    return redact_proxy(proxy) if proxy else "not configured"


def extractor_args() -> list[str]:
    """--extractor-args flags assembled from settings.

    TIKTOK_DEVICE_ID is surfaced as its own setting because it is the one
    that matters in practice: without app info of some kind, yt-dlp never
    even attempts TikTok's mobile API and goes straight to scraping the web
    page (see TikTokIE._real_extract). YTDLP_EXTRACTOR_ARGS is the raw
    passthrough for everything else.
    """
    settings = get_settings()
    flags: list[str] = []
    if settings.TIKTOK_DEVICE_ID:
        flags += ["--extractor-args", f"tiktok:device_id={settings.TIKTOK_DEVICE_ID}"]
    for spec in settings.YTDLP_EXTRACTOR_ARGS.split(";"):
        spec = spec.strip()
        if spec:
            flags += ["--extractor-args", spec]
    return flags


def _run(
    args: list[str],
    timeout: int,
    *,
    use_cookies: bool = True,
    impersonate: str | None = None,
) -> subprocess.CompletedProcess:
    """Invoke yt-dlp.

    `use_cookies=False` forces an anonymous request even when COOKIES_FILE is
    configured — the second attempt of the extraction matrix, and the only way
    to tell a stale session apart from a server-level block.

    `impersonate` forces a TLS browser fingerprint rather than leaving it to
    the extractor to request one.
    """
    settings = get_settings()
    cookies_file = settings.COOKIES_FILE if use_cookies else ""
    cmd = ["yt-dlp", *extractor_args()]
    if settings.YTDLP_PROXY:
        cmd += ["--proxy", settings.YTDLP_PROXY]
    if impersonate:
        cmd += ["--impersonate", impersonate]
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
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        # Comment extraction also logs stderr directly, without YtDlpError.
        # Sanitize here as well as at the exception boundary.
        if settings.YTDLP_PROXY and result.stderr:
            result.stderr = result.stderr.replace(
                settings.YTDLP_PROXY.encode(), redact_proxy(settings.YTDLP_PROXY).encode()
            )
        return result
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired.__str__ includes the complete command and therefore
        # --proxy credentials. Retain captured diagnostics, never that command.
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        raise YtDlpError(
            "download_failed", f"yt-dlp timed out after {timeout}s", stderr=stderr
        ) from None
    except FileNotFoundError as exc:
        raise YtDlpError("download_failed", "yt-dlp is not installed", stderr=str(exc)) from exc
    finally:
        if cookies_scratch_path:
            Path(cookies_scratch_path).unlink(missing_ok=True)


def get_version() -> str:
    proc = _run(["--version"], timeout=30)
    return proc.stdout.decode(errors="replace").strip()


@lru_cache(maxsize=1)
def impersonation_available() -> bool:
    """True when yt-dlp has at least one usable impersonate target.

    Cached for the life of the process: it is a property of the installed
    binary, and it is consulted on every extraction. Without the cache each
    job pays an extra yt-dlp subprocess just to ask the same question.
    update_to_channel() clears it, since an update can change the answer.
    """
    try:
        proc = _run(["--list-impersonate-targets"], timeout=30)
    except Exception:  # noqa: BLE001 - diagnostics must never break extraction
        return False
    output = proc.stdout.decode(errors="replace")
    # Output shape (yt-dlp 2026.07.04):
    #   [info] Available impersonate targets
    #   Client          OS           Source
    #   --------------------------------------
    #   Chrome-133      Macos-15     curl_cffi
    # Targets are still listed when the backend that implements them is
    # missing, but such rows are marked unavailable. Match on the "Source"
    # column carrying an actual backend name rather than on line position:
    # the header, the dashed rule and any [info]/[warning] lines must not
    # be mistaken for a usable target.
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("[") or set(stripped) <= {"-"}:
            continue
        columns = stripped.split()
        if len(columns) < 3 or columns[-1].lower() == "source":
            continue
        if "unavailable" in stripped.lower():
            continue
        return True
    return False


def impersonate_targets() -> list[str]:
    """Usable impersonate targets, as reported by yt-dlp itself. Empty when
    curl_cffi is missing — which is the state the health endpoint exists to
    make visible."""
    try:
        proc = _run(["--list-impersonate-targets"], timeout=30)
    except Exception:  # noqa: BLE001 - diagnostics must never break anything
        return []
    targets = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("[") or set(stripped) <= {"-"}:
            continue
        columns = stripped.split()
        if len(columns) < 3 or columns[-1].lower() == "source" or "unavailable" in stripped.lower():
            continue
        targets.append(columns[0])
    return targets


def _pip_install(spec: str, timeout: int, *, pre: bool = False) -> subprocess.CompletedProcess | None:
    """Returns None when pip couldn't be run at all (missing or timed out)."""
    args = ["pip", "install", "--upgrade"] + (["--pre"] if pre else []) + [spec]
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def update_to_channel(channel: str, log: callable) -> dict:
    """Install yt-dlp from the given release channel. Never raises.

    Called at worker startup or by an explicit admin action — deliberately not
    from inside a job. Swapping the binary mid-job changed the tool under
    running work, and the "confirm you are on the latest version" line that
    motivated doing it appears on nearly every TikTok failure regardless of
    whether the version is actually stale.
    """
    settings = get_settings()
    before = get_version()
    nightly = channel == "nightly"
    spec = "yt-dlp[default,curl-cffi]"

    log("info", f"Updating yt-dlp to the {channel} channel (currently {before})…")
    proc = _pip_install(spec, settings.YTDLP_UPDATE_TIMEOUT_SECONDS, pre=nightly)
    if proc is None or proc.returncode != 0:
        log("warning", f"yt-dlp update failed; continuing with the installed version {before}")
        return {"ok": False, "channel": channel, "before": before, "after": before}

    impersonation_available.cache_clear()
    after = get_version()
    if after != before:
        log("info", f"yt-dlp updated: {before} -> {after} ({channel})")
    else:
        log("info", f"yt-dlp already current on {channel} ({after})")
    if not impersonation_available():
        log(
            "warning",
            "yt-dlp has no browser-impersonation target available (curl_cffi missing). "
            "TikTok and some Instagram requests need it.",
        )
    return {"ok": True, "channel": channel, "before": before, "after": after}


def maybe_update_on_startup(log: callable) -> dict | None:
    """Honour YTDLP_UPDATE_ON_STARTUP. Returns None when disabled."""
    settings = get_settings()
    if not settings.YTDLP_UPDATE_ON_STARTUP:
        return None
    return update_to_channel(settings.YTDLP_CHANNEL, log)


def _preferred_impersonate_target() -> str | None:
    """The configured target, if yt-dlp actually has it available.

    Passing --impersonate with no curl_cffi installed makes yt-dlp exit
    immediately, which would turn a recoverable extraction into a hard
    failure — so this is gated on a real capability check.
    """
    settings = get_settings()
    if not settings.YTDLP_IMPERSONATE_TARGET:
        return None
    return settings.YTDLP_IMPERSONATE_TARGET if impersonation_available() else None


def _run_with_extractor_retry(args: list[str], timeout: int, log: callable) -> subprocess.CompletedProcess:
    """Run yt-dlp through a bounded attempt matrix.

    Attempt 1: configured cookies + browser impersonation.
    Attempt 2: no cookies, same impersonation — only when attempt 1 failed in
               a way where credentials could plausibly be the cause.

    Two attempts, no more. TikTok rate-limits aggressively, and hammering it
    turns a recoverable failure into a durable block. Notably, self-updating
    yt-dlp mid-job is gone: it never fixed anything, it changed the binary
    under a running job, and the "confirm you are on the latest version"
    boilerplate that motivated it appears on almost every TikTok failure.
    Version management now happens at startup (see maybe_update_on_startup).
    """
    impersonate = _preferred_impersonate_target()
    cookies_in_play = bool(cookies_configured())

    proc = _run(args, timeout, impersonate=impersonate)
    if proc.returncode == 0:
        return proc

    stderr = proc.stderr.decode(errors="replace")
    first = classify(stderr, used_cookies=cookies_in_play)

    should_retry_anonymously = (
        cookies_in_play and not first.terminal and first.code in RETRY_ANONYMOUSLY
    )
    if not should_retry_anonymously:
        raise YtDlpError(first.code, first.message, stderr=stderr)

    log(
        "warning",
        f"Extraction failed with the configured cookies ({first.code}); "
        "retrying once anonymously to tell a stale session apart from a server-level block.",
    )
    # A short pause: back-to-back requests after a challenge page are the
    # fastest way to earn a rate limit.
    time.sleep(get_settings().YTDLP_RETRY_DELAY_SECONDS)

    anon = _run(args, timeout, use_cookies=False, impersonate=impersonate)
    if anon.returncode == 0:
        log(
            "warning",
            "Anonymous retry succeeded — the configured TikTok cookies are stale or "
            "rejected. Replace COOKIES_FILE with a fresh export.",
        )
        return anon

    anon_stderr = anon.stderr.decode(errors="replace")
    verdict = reconcile(first, classify(anon_stderr, used_cookies=False))
    log("error", f"Both authenticated and anonymous extraction failed ({verdict.code}).")
    raise YtDlpError(verdict.code, verdict.message, stderr=anon_stderr)


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


def cookies_configured() -> bool:
    """True when a usable cookie file is actually present on disk."""
    cookies_file = get_settings().COOKIES_FILE
    return bool(cookies_file) and Path(cookies_file).is_file()


# Cookie names TikTok sets on a signed-in session. Presence is checked; values
# are never read, logged, or returned anywhere.
_TIKTOK_COOKIE_NAMES = ("sessionid", "sid_tt", "tt_webid", "ttwid", "msToken")


def cookie_file_report() -> dict:
    """Sanitized description of the cookie file: shape and age, never content.

    Deliberately returns no cookie values, no domains beyond a boolean, and no
    file excerpt — this feeds a diagnostics endpoint and job logs, both of
    which are visible to anyone who can see the app.
    """
    cookies_file = get_settings().COOKIES_FILE
    if not cookies_file:
        return {"configured": False, "present": False, "reason": "COOKIES_FILE is not set"}
    path = Path(cookies_file)
    if not path.is_file():
        return {
            "configured": True,
            "present": False,
            "reason": "COOKIES_FILE is set but no file exists at that path",
        }

    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"configured": True, "present": True, "readable": False, "reason": str(exc)}

    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    netscape = text.lstrip().startswith("# Netscape HTTP Cookie File") or all(
        ln.count("\t") >= 6 for ln in lines[:5]
    ) if lines else False
    parsed = [ln.split("\t") for ln in lines if ln.count("\t") >= 6]
    tiktok_rows = [parts for parts in parsed if "tiktok" in parts[0].lower()]
    has_tiktok = bool(tiktok_rows)
    # Names such as `sessionid` are shared by many sites. Only report them as
    # TikTok session cookies when their Netscape-cookie domain is TikTok;
    # otherwise an Instagram-only jar looks like valid TikTok authentication.
    names = {parts[5] for parts in tiktok_rows}
    age_days = round((time.time() - stat.st_mtime) / 86400, 1)

    return {
        "configured": True,
        "present": True,
        "readable": True,
        "netscape_format": bool(netscape),
        "entry_count": len(lines),
        "has_tiktok_entries": has_tiktok,
        # Names only — never values.
        "tiktok_session_cookies_present": sorted(n for n in names if n in _TIKTOK_COOKIE_NAMES),
        "modified_age_days": age_days,
        # TikTok sessions go stale in weeks, not months; a very old export is
        # worth flagging before it is blamed on the extractor.
        "likely_stale": age_days > 30,
        "world_readable": bool(stat.st_mode & 0o004),
    }


def cookies_status() -> str:
    """One line for the job log. Includes the export's age because a rejected
    session is the failure mode that actually bites, and it looks identical to
    a bot-check page — knowing the jar is two months old up front saves
    diagnosing the extractor instead."""
    cookies_file = get_settings().COOKIES_FILE
    if not cookies_file:
        return "not configured"
    if not Path(cookies_file).is_file():
        return f"configured ({cookies_file}) but file not found"

    report = cookie_file_report()
    age = report.get("modified_age_days")
    if age is None:
        return "in use"
    suffix = " — old enough that TikTok may reject it" if report.get("likely_stale") else ""
    return f"in use (export is {age} days old){suffix}"


def extract_metadata(url: str, *, log: callable, log_cookie_status: bool = True) -> VideoMetadata:
    settings = get_settings()
    if log_cookie_status:
        log("info", f"yt-dlp cookies: {cookies_status()}")
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
        uploader_id=info.get("uploader_id") or info.get("channel_id"),
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


def search_videos(query: str, count: int, sort_mode: str, *, log: callable) -> list[dict]:
    """Research mode: flat YouTube search via yt-dlp's ytsearch/ytsearchdate
    pseudo-URLs (no YouTube Data API, no video downloads). Returns the flat
    playlist entries — enough for candidate ids/urls plus cheap prefiltering;
    authoritative metadata comes from a per-video extract_metadata() pass."""
    prefix = "ytsearchdate" if sort_mode == "newest" else "ytsearch"
    settings = get_settings()
    proc = _run_with_extractor_retry(
        ["--dump-single-json", "--flat-playlist", "--no-warnings", f"{prefix}{count}:{query}"],
        timeout=settings.YTDLP_METADATA_TIMEOUT_SECONDS,
        log=log,
    )
    info = json.loads(proc.stdout.decode(errors="replace"))
    return list(info.get("entries") or [])


def download_captions(
    url: str,
    dest_dir: Path,
    video_id: str,
    *,
    manual: bool,
    log: callable,
    sub_langs: str | None = None,
) -> Path | None:
    """Download only the subtitle file for one video (never the video itself).
    manual=True fetches creator-provided subtitles; manual=False fetches
    auto-generated captions. `sub_langs` is yt-dlp --sub-langs syntax and
    defaults to research mode's English-only setting; transcript mode passes
    a wider selector so a non-English video isn't silently skipped. Returns
    the subtitle file path, or None if yt-dlp produced nothing."""
    settings = get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    _run_with_extractor_retry(
        [
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "--write-subs" if manual else "--write-auto-subs",
            "--sub-langs",
            sub_langs or settings.RESEARCH_SUB_LANGS,
            "--sub-format",
            "vtt/srt/best",
            "-o",
            str(dest_dir / "%(id)s"),
            url,
        ],
        timeout=settings.YTDLP_METADATA_TIMEOUT_SECONDS,
        log=log,
    )
    for pattern in (f"{video_id}*.vtt", f"{video_id}*.srt"):
        matches = sorted(dest_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def fetch_profile_reel_view_count(username: str, target_id: str, *, log: callable) -> int | None:
    """Best-effort fallback for Instagram specifically: a single Reel's own
    metadata response sometimes omits view_count even though the same
    number is visible on the account's Reels grid tab in a browser. Fetch
    that grid as a flat playlist and pull the matching entry's count.

    This rides entirely on yt-dlp's own understanding of the Instagram
    profile/Reels-listing page rather than hand-rolled HTML/JSON parsing,
    so it inherits the same cookie handling and self-update path as
    everything else — but whether the flat listing actually carries a
    view/play count per entry is genuinely unconfirmed without testing
    against a real account; never raises, only ever backfills a value it
    can positively confirm."""
    if not username:
        return None
    settings = get_settings()
    profile_url = f"https://www.instagram.com/{username}/reels/"
    try:
        proc = _run(
            ["--flat-playlist", "--dump-single-json", "--no-warnings", profile_url],
            timeout=settings.YTDLP_METADATA_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            log(
                "warning",
                f"Reels-grid view-count fallback couldn't fetch @{username}'s grid; continuing without it",
            )
            return None
        info = json.loads(proc.stdout.decode(errors="replace"))
        for entry in info.get("entries") or []:
            if str(entry.get("id")) == str(target_id):
                count = entry.get("view_count")
                if count is None:
                    count = entry.get("play_count")
                if count is not None:
                    log("info", f"Backfilled view_count={count} from @{username}'s Reels grid")
                else:
                    log(
                        "warning",
                        f"Found the reel in @{username}'s Reels grid listing, but it carries no "
                        "view/play count field either — this platform genuinely isn't exposing it here.",
                    )
                return count
        log(
            "warning",
            f"Reel {target_id} wasn't found in @{username}'s Reels grid listing "
            "(may be further back than the first page, or the account restricts grid visibility)",
        )
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort by design, never fails the job
        log("warning", f"Reels-grid view-count fallback failed: {exc}")
        return None


def download_audio(url: str, dest_dir: Path, *, log: callable) -> Path:
    """Downloads only the best audio stream, named 'audio.<ext>'.

    Used by transcript mode when a platform has no captions: Whisper only
    ever needs the audio, and skipping the video stream saves most of the
    bytes and most of the time. No ffmpeg post-processing is requested —
    whatever container the platform serves is fine, since the transcription
    step re-encodes to WAV anyway.
    """
    settings = get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    _run_with_extractor_retry(
        [
            "--no-warnings",
            "--no-playlist",
            "-f",
            "ba/bestaudio/b",
            "-o",
            str(dest_dir / "audio.%(ext)s"),
            url,
        ],
        timeout=settings.YTDLP_TIMEOUT_SECONDS,
        log=log,
    )
    matches = sorted(dest_dir.glob("audio.*"))
    if not matches:
        raise YtDlpError("download_failed", "yt-dlp reported success but no audio file was found")
    return matches[0]


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


# Platforms whose yt-dlp extractor has no comment support at all — attempting
# is pointless, so the status can say "unsupported" up front instead of
# reporting a misleading empty success.
_COMMENTS_UNSUPPORTED_PLATFORMS = {"tiktok"}

_RATE_LIMIT_MARKERS = ["rate-limit", "rate limit", "too many requests", "429"]
_AUTH_MARKERS = ["login required", "requires authentication", "sign in", "log in", "cookies", "account is private"]


def _classify_comment_failure(stderr: str) -> tuple[str, str]:
    """Returns (status, reason) for a failed comment-extraction attempt."""
    from app.utils import status as st

    lowered = stderr.lower()
    if any(m in lowered for m in _RATE_LIMIT_MARKERS):
        return st.RATE_LIMITED, "The platform rate-limited the request"
    if any(m in lowered for m in _AUTH_MARKERS):
        return st.AUTHENTICATION_REQUIRED, "The platform requires a logged-in session for comments"
    return st.EXTRACTION_FAILED, "yt-dlp could not extract comments"


def _normalize_comments(raw_comments: list[dict], limit: int) -> list[dict]:
    """Top-level comments ranked by likes, enriched with the fields yt-dlp
    actually provides (null when a platform doesn't expose one)."""
    reply_counts: dict[str, int] = {}
    for c in raw_comments:
        parent = c.get("parent")
        if parent and parent != "root":
            reply_counts[str(parent)] = reply_counts.get(str(parent), 0) + 1

    top_level = [c for c in raw_comments if c.get("parent") in (None, "root")]
    top_level.sort(key=lambda c: c.get("like_count") or 0, reverse=True)

    normalized = []
    for c in top_level[:limit]:
        comment_id = c.get("id")
        normalized.append(
            {
                "id": comment_id,
                "text": c.get("text"),
                "author": c.get("author"),
                "author_id": c.get("author_id"),
                "like_count": c.get("like_count"),
                "reply_count": reply_counts.get(str(comment_id)) if comment_id is not None else None,
                "timestamp": c.get("timestamp"),
                "is_pinned": c.get("is_pinned"),
                "author_is_uploader": c.get("author_is_uploader"),
            }
        )
    return normalized


def extract_comments(
    url: str,
    *,
    limit: int,
    log: callable,
    platform: str | None = None,
    platform_comment_count: int | None = None,
) -> dict:
    """Best-effort comment extraction with an explicit outcome — never raises
    and never hides *why* a result is empty. Returns
    {status, reason, error, platform_comment_count, extracted_comment_count,
     attempted_at, comments[]}."""
    from app.utils import status as st

    settings = get_settings()
    base = {
        "platform_comment_count": platform_comment_count,
        "extracted_comment_count": 0,
        "attempted_at": now_utc_iso(),
        "error": None,
        "comments": [],
    }

    if platform in _COMMENTS_UNSUPPORTED_PLATFORMS:
        log("warning", f"Comment extraction is unsupported for {platform} — recording status and moving on")
        return {
            **base,
            "status": st.UNSUPPORTED,
            "reason": f"yt-dlp has no comment extraction support for {platform}",
        }

    try:
        proc = _run(
            ["--dump-single-json", "--skip-download", "--write-comments", "--no-warnings", url],
            timeout=settings.YTDLP_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")
            failure_status, reason = _classify_comment_failure(stderr)
            log("warning", f"Comment extraction failed ({failure_status}): {_first_error_line(stderr)}")
            return {**base, "status": failure_status, "reason": reason, "error": _first_error_line(stderr)}
        info = json.loads(proc.stdout.decode(errors="replace"))
        raw_comments = info.get("comments") or []
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        log("warning", f"Comment extraction failed, continuing without comments: {exc}")
        return {**base, "status": st.EXTRACTION_FAILED, "reason": "Unexpected error during extraction", "error": str(exc)}

    comments = _normalize_comments(raw_comments, limit)
    if not comments:
        # R1.5 status classification:
        # - platform reports 0 and we got 0        -> no_comments (clean success)
        # - platform reports N>0 but we got 0:
        #     * on a platform where the current extractor has no
        #       public_comment_text capability (e.g. TikTok), this is
        #       "unsupported" — not a failure to fetch, but the wrong tool
        #       for the job.
        #     * everywhere else it's unexpected_empty_result (commonly
        #       auth/rate-limit).
        if platform_comment_count == 0:
            return {**base, "status": st.NO_COMMENTS, "reason": "The platform reports zero comments"}
        if platform_comment_count and platform_comment_count > 0:
            from app.utils.platform_capabilities import capabilities_for

            caps = capabilities_for(platform)
            if caps.get("public_comment_text") is False:
                log(
                    "warning",
                    f"Platform reports {platform_comment_count} comments but the current yt-dlp "
                    f"extractor for {platform} does not support comment text",
                )
                return {
                    **base,
                    "status": st.UNSUPPORTED,
                    "reason": (
                        f"Comment text is not supported by the current yt-dlp extractor for {platform}"
                    ),
                }
            log(
                "warning",
                f"Platform reports {platform_comment_count} comments but extraction returned none "
                "(commonly authentication or rate limiting)",
            )
            return {
                **base,
                "status": st.UNEXPECTED_EMPTY_RESULT,
                "reason": "zero_results_unexpected",
                "error": (
                    f"The platform reports {platform_comment_count} comments but extraction "
                    "returned none — commonly caused by authentication requirements or rate limiting"
                ),
            }
        return {**base, "status": st.SUCCESS, "reason": "no_comments_returned"}

    return {
        **base,
        "status": st.SUCCESS,
        "reason": None,
        "extracted_comment_count": len(comments),
        "comments": comments,
    }
