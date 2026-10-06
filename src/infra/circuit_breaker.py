from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("circuit_breaker")

DEFAULT_MAX_FAILURES = 3
DEFAULT_EXPIRY_HOURS = 48


def _load(failure_path: Path) -> list[dict]:
    """Load entries as [{uuid, ts}, ...]. Supports legacy bare-UUID format."""
    if not failure_path.exists():
        return []
    entries = []
    for line in failure_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            parts = line.split("|", 1)
            entries.append({"uuid": parts[0], "ts": float(parts[1])})
        else:
            # Legacy format: bare UUID, treat as old (will expire)
            entries.append({"uuid": line, "ts": 0.0})
    return entries


def _save(failure_path: Path, entries: list[dict]) -> None:
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{e['uuid']}|{e['ts']}" for e in entries]
    failure_path.write_text("\n".join(lines) + "\n" if lines else "")


def _prune_expired(entries: list[dict], expiry_hours: float = DEFAULT_EXPIRY_HOURS) -> list[dict]:
    """Remove entries older than expiry_hours. Legacy entries (ts=0) always expire."""
    cutoff = time.time() - (expiry_hours * 3600)
    return [e for e in entries if e["ts"] > cutoff]


def get_failures(
    uuid: str, failure_path: str | Path, expiry_hours: float = DEFAULT_EXPIRY_HOURS
) -> int:
    entries = _prune_expired(_load(Path(failure_path)), expiry_hours)
    return sum(1 for e in entries if e["uuid"] == uuid)


def is_tripped(
    uuid: str,
    failure_path: str | Path,
    max_failures: int = DEFAULT_MAX_FAILURES,
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
) -> bool:
    count = get_failures(uuid, failure_path, expiry_hours)
    if count >= max_failures:
        log.warning(f"Circuit tripped for {uuid[:8]}: {count}/{max_failures}")
        return True
    return False


def record_failure(uuid: str, failure_path: str | Path) -> int:
    path = Path(failure_path)
    entries = _load(path)
    entries.append({"uuid": uuid, "ts": time.time()})
    _save(path, entries)
    count = sum(1 for e in entries if e["uuid"] == uuid)
    log.info(f"Failure recorded for {uuid[:8]}: {count}")
    return count


def clear_failures(uuid: str, failure_path: str | Path) -> None:
    path = Path(failure_path)
    entries = _load(path)
    cleaned = [e for e in entries if e["uuid"] != uuid]
    _save(path, cleaned)
    log.info(f"Cleared failures for {uuid[:8]}")


def get_tripped_tasks(
    failure_path: str | Path,
    max_failures: int = DEFAULT_MAX_FAILURES,
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
) -> list[dict]:
    """Return list of {uuid, count, last_failure_ts} for all circuit-broken tasks."""
    entries = _prune_expired(_load(Path(failure_path)), expiry_hours)
    counts: dict[str, dict] = {}
    for e in entries:
        uid = e["uuid"]
        if uid not in counts:
            counts[uid] = {"uuid": uid, "count": 0, "last_failure_ts": 0.0}
        counts[uid]["count"] += 1
        counts[uid]["last_failure_ts"] = max(counts[uid]["last_failure_ts"], e["ts"])
    return [v for v in counts.values() if v["count"] >= max_failures]
