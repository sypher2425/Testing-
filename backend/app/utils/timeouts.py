"""Timeout + heartbeat helpers for long blocking pipeline calls.

Round 1.6 context: `WHISPER_TIMEOUT_SECONDS` was declared in config but never
actually applied to anything, so a stalled model download or a wedged
transcription could hang a job forever. ffmpeg calls get their timeout from
`subprocess.run(timeout=…)`, but faster-whisper runs in-process, so it needs
a different mechanism.

`time_limit()` uses SIGALRM, which is valid here because Celery's prefork
pool runs each task in the *main thread of a worker child process*. Caveat
worth knowing: Python only delivers signals between bytecodes, so a long
native call (ctranslate2 compute, a socket read inside huggingface_hub) can
delay the alarm until it returns. That's why this is only one of three
layers — `HF_HUB_DOWNLOAD_TIMEOUT` bounds the download socket itself, and
Celery's own `task_time_limit` hard-kills the child as a last resort.
"""
import re
import signal
import threading
from contextlib import contextmanager


class StepTimeout(Exception):
    """Raised when a guarded block exceeds its time limit."""


@contextmanager
def time_limit(seconds: int | float | None, message: str):
    """Bound a blocking call with SIGALRM. A non-positive/None limit disables
    the guard. Safely degrades to a no-op when SIGALRM is unavailable (Windows)
    or when not on the main thread (e.g. pytest-xdist or an eager Celery task)."""
    if (
        not seconds
        or seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def _on_alarm(signum, frame):  # noqa: ARG001
        raise StepTimeout(message)

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    # setitimer accepts floats; alarm() would truncate to whole seconds.
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class HeartbeatTicker:
    """Background thread that calls `callback` every `interval` seconds while
    a blocking call runs, so the job's last_heartbeat stays fresh and the
    stale-job reaper doesn't kill work that's actually progressing.

    Use as a context manager:
        with HeartbeatTicker(10, ctx.heartbeat):
            model = load_model()
    """

    def __init__(self, interval: float, callback):
        # Floor guards against a pathological 0 spinning the thread; kept
        # small enough that tests can use sub-second intervals.
        self.interval = max(float(interval), 0.05)
        self.callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.callback()
            except Exception:  # noqa: BLE001 - a heartbeat must never break the work it guards
                pass

    def __enter__(self) -> "HeartbeatTicker":
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


_SIGNAL_RE = re.compile(r"signal\s+(\d+)")
_EXITCODE_RE = re.compile(r"exitcode\s+(-?\d+)")


def parse_worker_exit(detail: str) -> tuple[int | None, int | None]:
    """Pull (signal_number, exitcode) out of a Celery WorkerLostError message.

    Celery formats these like "Worker exited prematurely: signal 9 (SIGKILL)
    Job: 0." or "... exitcode 155.", so the fields must be matched by name —
    naively grabbing the first integer in the string reads "signal 9" as an
    exit code and loses the SIGKILL/OOM signal entirely.
    """
    if not detail:
        return None, None
    signal_match = _SIGNAL_RE.search(detail)
    exit_match = _EXITCODE_RE.search(detail)
    return (
        int(signal_match.group(1)) if signal_match else None,
        int(exit_match.group(1)) if exit_match else None,
    )


def describe_worker_exit(exitcode: int | None = None, signal_number: int | None = None) -> str | None:
    """Human-readable explanation for an abnormal worker-child exit.
    Negative exit codes are signals (multiprocessing convention); 128+N is the
    shell convention. SIGKILL (9) on a transcription worker is nearly always
    the OOM killer."""
    if exitcode is None and signal_number is None:
        return None
    if signal_number is None and exitcode is not None:
        if exitcode < 0:
            signal_number = -exitcode
        elif exitcode > 128:
            signal_number = exitcode - 128

    if signal_number == signal.SIGKILL:
        return (
            "worker child was killed with SIGKILL (9) — this is almost always the out-of-memory "
            "killer. Lower TRANSCRIPTION_CONCURRENCY, use a smaller WHISPER_MODEL_SIZE, or raise "
            "the memory limit available to Docker."
        )
    if signal_number == signal.SIGTERM:
        return "worker child was terminated with SIGTERM (15) — usually a shutdown or a revoked task."
    if signal_number is not None:
        return f"worker child exited on signal {signal_number}."
    if exitcode is not None and exitcode != 0:
        return f"worker child exited with code {exitcode}."
    return None
