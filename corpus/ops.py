"""Running corpus unattended: one writer at a time, clean shutdown, bounded disk.

Everything here exists because the nightly job runs with nobody watching. Each
helper answers a failure that is silent when it happens and expensive later:

  * two runs racing on one database (a timer firing while the dashboard's enrich
    job is still going, or a manual sync during the nightly one) both open
    'running' rows, both bill Apify for the same accounts, and one of them dies
    on "database is locked" half way through its writes;
  * systemd stops a unit with SIGTERM, which Python does not turn into an
    exception, so `finally` blocks never ran and scrape_run rows stayed 'running'
    forever, indistinguishable from a run still in progress;
  * temp directories and cached thumbnails that nothing ever removes.
"""
from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

# sysexits.h EX_TEMPFAIL: "try again later". systemd and cron wrappers treat it as
# a transient failure, which is exactly what "another run holds the lock" is.
EX_TEMPFAIL = 75

# SIGTERM's conventional exit status (128 + 15).
EX_TERMINATED = 143


class LockBusy(RuntimeError):
    """Another corpus process holds the database lock."""


class Terminated(KeyboardInterrupt):
    """SIGTERM, delivered as an interrupt so every `finally` and `_sweep` runs.

    A KeyboardInterrupt subclass on purpose: every cleanup path in the codebase
    already catches KeyboardInterrupt/BaseException to close its run rows, so this
    reuses all of them instead of adding a second shutdown path to keep in step.
    """


def lock_path(db_path: str | Path) -> Path:
    """The lock lives next to the database it protects, so `--db` on a temp file
    never contends with the real corpus and two corpora never share a lock."""
    return Path(db_path).expanduser().parent / ".lock"


@contextmanager
def run_lock(db_path: str | Path, *, what: str = "corpus"):
    """Hold an exclusive, non-blocking flock for the duration of the block.

    flock rather than a pid file: the kernel drops it when the process dies, even
    on SIGKILL or a power cut, so a crashed run can never wedge the next one.
    Raises LockBusy immediately (no waiting) when another process holds it; the
    message names that process so the operator knows what to look at.
    """
    import fcntl

    p = lock_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                holder = os.pread(fd, 200, 0).decode(errors="replace").strip()
            except OSError:
                pass
            raise LockBusy(
                f"another corpus run holds {p}"
                + (f" ({holder})" if holder else "")
                + "; not starting a second writer. Try again when it finishes.")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}: {what}, since "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S')}".encode(), 0)
        try:
            yield p
        finally:
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def sigterm_as_interrupt():
    """While inside, SIGTERM raises Terminated in the main thread; the previous
    handler is restored on exit, so main() called as a library (tests, the
    dashboard) leaves the process's signal handling as it found it. A no-op off
    the main thread, where signal.signal raises."""
    def _raise(signum, frame):
        raise Terminated()
    try:
        prev = signal.signal(signal.SIGTERM, _raise)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)


# ── disk hygiene ────────────────────────────────────────────────────────────

# Prefixes of every temp dir corpus creates. A SIGKILL (or the OOM killer) skips
# TemporaryDirectory's cleanup, and a downloaded reel is tens of megabytes.
TMP_PREFIXES = ("corpus-tx-", "corpus-media-", "corpus-img-")


def sweep_tmp(*, older_than_s: int = 6 * 3600, tmp_dir: str | Path | None = None) -> int:
    """Remove corpus temp dirs older than `older_than_s`. Returns how many went.

    Old ones only: a concurrent run (the dashboard, say) may be inside a fresh one
    right now, and the lock does not cover read-only commands.
    """
    root = Path(tmp_dir or tempfile.gettempdir())
    cutoff = time.time() - older_than_s
    n = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for p in entries:
        if not p.name.startswith(TMP_PREFIXES):
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            n += 1
        except OSError:
            continue
    return n


# The brief's downscaled thumbnails are keyed by source path + mtime, so every
# re-encode or moved media root strands the old entry for good. Sixty days keeps
# everything a brief written this quarter needs; a pruned entry costs one ffmpeg
# call to regenerate.
THUMB_MAX_AGE_DAYS = 60


def prune_thumbs(thumb_dir: str | Path, *, days: int = THUMB_MAX_AGE_DAYS) -> int:
    """Delete cached thumbnails not written in `days`. Returns the count."""
    d = Path(thumb_dir).expanduser()
    if not d.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in d.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n
