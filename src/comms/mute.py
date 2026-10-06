"""Incident mute switch — scoped, TTL'd silence for SYMPTOM alert rails.

State file: ~/.odin/mute-until.json  {"until": <epoch>, "reason": "<why>"}
Set by writing that file on each alert-emitting host (the helper script that
does this is not part of this repo). The TTL is mandatory and capped at 24h, enforced at
READ time: an expired, over-long, or malformed file counts as not muted —
forgetting to unmute can never permanently silence alerting.

Dead-man rails are exempt by contract: callers carrying absence-of-success
semantics (odin-watchdog and anything else that pages on silence) pass
deadman=True and are never muted. Muting exists to quench symptom fan-out
during a known incident or maintenance window, not to disarm dead-men.
Muted alerts are still logged with a [MUTED] prefix so nothing is lost.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("odin.mute")

MUTE_FILE = Path.home() / ".odin" / "mute-until.json"
MAX_TTL_SECONDS = 24 * 3600


def mute_state() -> dict | None:
    """Return {"until": epoch, "reason": str} if an unexpired mute is active."""
    try:
        data = json.loads(MUTE_FILE.read_text())
        until = float(data.get("until", 0))
    except (OSError, ValueError, TypeError):
        return None
    now = time.time()
    if now < until <= now + MAX_TTL_SECONDS:
        return {"until": until, "reason": str(data.get("reason", ""))}
    return None


def is_muted(deadman: bool = False) -> bool:
    """True if symptom alerts are muted. Dead-man callers are never muted."""
    if deadman:
        return False
    state = mute_state()
    if state:
        log.info(
            "[MUTED] alerts muted until %s (%s)",
            time.strftime("%H:%M", time.localtime(state["until"])),
            state["reason"],
        )
        return True
    return False
