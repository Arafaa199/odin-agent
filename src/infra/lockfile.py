from __future__ import annotations

import atexit
import fcntl
import logging
import os
import signal
import time
from pathlib import Path

log = logging.getLogger("lockfile")

STALE_MINUTES = 30

_lock_fd = None


def acquire(lock_path: str | Path, force: bool = False) -> bool:
    global _lock_fd
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if force and lock_path.exists():
        log.info("Force mode — removing existing lockfile")
        try:
            lock_path.unlink()
        except OSError:
            pass

    try:
        _lock_fd = open(lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        # Check if the lock is stale (holder died without releasing)
        if lock_path.exists():
            try:
                age_minutes = (time.time() - lock_path.stat().st_mtime) / 60
                if age_minutes >= STALE_MINUTES:
                    log.info(f"Stale lock ({age_minutes:.0f}min old) — breaking")
                    lock_path.unlink()
                    _lock_fd = open(lock_path, "w")
                    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    log.warning(f"Lock held by another process ({age_minutes:.0f}min old)")
                    return False
            except (IOError, OSError):
                log.warning("Lock contention — another process holds the lock")
                return False
        else:
            return False

    # Write PID for observability (not used for locking — fcntl handles that)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()

    def _cleanup():
        global _lock_fd
        try:
            if _lock_fd:
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
                _lock_fd.close()
                _lock_fd = None
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), exit(143)))

    log.debug(f"Lock acquired: {lock_path}")
    return True


def release(lock_path: str | Path) -> None:
    global _lock_fd
    lock_path = Path(lock_path)
    try:
        if _lock_fd:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
        if lock_path.exists():
            lock_path.unlink()
            log.debug(f"Lock released: {lock_path}")
    except OSError:
        pass
