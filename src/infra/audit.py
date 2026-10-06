"""Append-only JSONL audit log with HMAC integrity chain."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

DEFAULT_AUDIT_PATH = "~/.local/state/odin/audit.jsonl"
AUDIT_PATH_ENV = "ODIN_AUDIT_LOG"
HMAC_KEY_ENV = "ODIN_AUDIT_KEY"


class AuditKeyMissing(RuntimeError):
    pass


def _get_key() -> bytes:
    # Fail closed: a well-known default key would let anyone re-sign a tampered log.
    key = os.environ.get(HMAC_KEY_ENV)
    if not key:
        raise AuditKeyMissing(f"{HMAC_KEY_ENV} is not set; refusing to sign or verify the audit log")
    return key.encode()


def require_key() -> None:
    """Raise AuditKeyMissing now instead of on the first write."""
    _get_key()


def _hmac_sign(data: str) -> str:
    return hmac.new(_get_key(), data.encode(), hashlib.sha256).hexdigest()[:16]


def _default_path() -> Path:
    return Path(os.environ.get(AUDIT_PATH_ENV, DEFAULT_AUDIT_PATH)).expanduser()


def _last_hash(path: Path) -> str:
    """Get hash of last line for chain integrity."""
    if not path.exists():
        return "genesis"
    try:
        lines = path.read_text().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if line:
                return hashlib.sha256(line.encode()).hexdigest()[:16]
    except Exception:
        pass
    return "genesis"


def append(event: dict, log_path: str | Path | None = None) -> None:
    path = Path(log_path).expanduser() if log_path else _default_path()
    prev_hash = _last_hash(path)
    event = {
        "_ts": time.time(),
        "_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "_prev": prev_hash,
        **event,
    }
    payload = json.dumps(event, default=str, sort_keys=True)
    sig = _hmac_sign(payload)
    signed_line = f"{payload}\t{sig}"

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(signed_line + "\n")


def verify(log_path: str | Path | None = None) -> tuple[bool, list[str]]:
    """Verify audit log integrity. Returns (ok, list_of_errors)."""
    path = Path(log_path).expanduser() if log_path else _default_path()
    if not path.exists():
        return True, []

    errors = []
    prev_hash = "genesis"

    for i, raw_line in enumerate(path.read_text().splitlines(), 1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        parts = raw_line.rsplit("\t", 1)
        if len(parts) != 2:
            errors.append(f"Line {i}: missing HMAC signature")
            continue

        payload, sig = parts
        expected_sig = _hmac_sign(payload)
        if sig != expected_sig:
            errors.append(f"Line {i}: HMAC mismatch (tampering?)")
            continue

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            errors.append(f"Line {i}: invalid JSON")
            continue

        if event.get("_prev") != prev_hash:
            errors.append(
                f"Line {i}: chain break (_prev={event.get('_prev')}, expected={prev_hash})"
            )

        prev_hash = hashlib.sha256(raw_line.encode()).hexdigest()[:16]

    return len(errors) == 0, errors


def read_events(log_path: str | Path | None = None, stage: str | None = None) -> list[dict]:
    path = Path(log_path).expanduser() if log_path else _default_path()
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip HMAC signature if present
        parts = line.rsplit("\t", 1)
        payload = parts[0]
        try:
            evt = json.loads(payload)
            if stage is None or evt.get("stage") == stage:
                events.append(evt)
        except json.JSONDecodeError:
            continue
    return events
