"""Cartographer Drift Detector — compares current registry against previous state.

Detects:
- New undocumented services (not in known_services.yaml)
- Newly unhealthy services (state changed to failed/not_found)
- Recovered services (was unhealthy, now healthy)
- Disappeared services (was present, now gone)
- New services (first time seen)

Only alerts on state transitions — no repeated alerts for same condition.
State persisted to drift-state.json between runs.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import intermittent_hosts

log = logging.getLogger("cartographer.drift")

STATE_DIR = Path.home() / ".odin" / ".cartographer"
DRIFT_STATE_PATH = STATE_DIR / "drift-state.json"

RESOLVED_EXPIRY_HOURS = 24


def _svc_key(svc: dict) -> str:
    return f"{svc['host']}:{svc['type']}:{svc['name']}"


def load_drift_state(path: Optional[Path] = None) -> dict:
    path = path or DRIFT_STATE_PATH
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt drift state — starting fresh")
    return {"services": {}, "last_run": None}


def save_drift_state(state: dict, path: Optional[Path] = None) -> None:
    path = path or DRIFT_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str))


def _is_device_unreachable(registry: dict, device: str) -> bool:
    """Check if a device is unreachable or sleeping.

    Triggers on SSH timeout OR when every documented service on the device
    reports not_found (macOS lid-down: SSH responds via Power Nap but
    launchctl returns nothing).
    """
    for err in registry.get("scan_errors", []):
        if isinstance(err, str) and device in err and "timed out" in err.lower():
            return True
        if isinstance(err, dict) and err.get("host") == device:
            return True

    documented = [
        s for s in registry.get("services", []) if s.get("host") == device and s.get("documented")
    ]
    if documented and all(s.get("state") == "not_found" for s in documented):
        log.info(
            "All %d documented services on %s are not_found — treating as sleeping",
            len(documented),
            device,
        )
        return True

    return False


def detect_drift(registry: dict, prev_state: Optional[dict] = None) -> dict:
    if prev_state is None:
        prev_state = load_drift_state()

    now = datetime.now(timezone.utc).isoformat()
    prev_services = prev_state.get("services", {})
    is_first_run = prev_state.get("last_run") is None

    unreachable_intermittent = {
        dev for dev in intermittent_hosts() if _is_device_unreachable(registry, dev)
    }
    if unreachable_intermittent:
        log.info(
            "Intermittent devices unreachable (suppressing drift): %s",
            ", ".join(sorted(unreachable_intermittent)),
        )

    current_services = {}
    for svc in registry.get("services", []):
        key = _svc_key(svc)
        current_services[key] = {
            "name": svc["name"],
            "host": svc["host"],
            "type": svc["type"],
            "state": svc.get("state", "unknown"),
            "documented": svc.get("documented", False),
            "healthy": svc.get("healthy"),
            "category": svc.get("category", "unknown"),
            "port": svc.get("port"),
            "process": svc.get("process"),
        }

    new_undocumented = []
    newly_unhealthy = []
    recovered = []
    disappeared = []
    new_services = []

    for key, curr in current_services.items():
        if curr["host"] in unreachable_intermittent:
            continue

        prev = prev_services.get(key)

        if prev is None:
            if is_first_run:
                continue
            new_services.append(curr)
            if not curr["documented"]:
                new_undocumented.append(curr)
            continue

        prev_healthy = prev.get("healthy")
        curr_healthy = curr.get("healthy")

        if prev_healthy is not False and curr_healthy is False:
            newly_unhealthy.append(curr)

        if prev_healthy is False and curr_healthy is True:
            recovered.append(curr)

        alerted = prev.get("alerted_undocumented")
        if not curr["documented"] and not alerted and not is_first_run:
            new_undocumented.append(curr)

    for key, prev in prev_services.items():
        if prev.get("host") in unreachable_intermittent:
            continue
        if key not in current_services:
            if prev.get("documented") and prev.get("state") != "not_found":
                disappeared.append(prev)

    new_state = {
        "services": {},
        "last_run": now,
    }
    for key, curr in current_services.items():
        entry = dict(curr)
        prev = prev_services.get(key, {})

        if not curr["documented"]:
            if is_first_run:
                entry["alerted_undocumented"] = now
            else:
                entry["alerted_undocumented"] = prev.get("alerted_undocumented") or (
                    now if key in {_svc_key(s) for s in new_undocumented} else None
                )

        if curr.get("healthy") is False:
            entry["unhealthy_since"] = prev.get("unhealthy_since") or now
        else:
            entry.pop("unhealthy_since", None)

        new_state["services"][key] = entry

    drift_report = {
        "generated_at": now,
        "is_first_run": is_first_run,
        "new_undocumented": new_undocumented,
        "newly_unhealthy": newly_unhealthy,
        "recovered": recovered,
        "disappeared": disappeared,
        "new_services": new_services,
        "counts": {
            "new_undocumented": len(new_undocumented),
            "newly_unhealthy": len(newly_unhealthy),
            "recovered": len(recovered),
            "disappeared": len(disappeared),
            "new_services": len(new_services),
        },
    }

    return {
        "report": drift_report,
        "new_state": new_state,
    }


def has_actionable_drift(report: dict) -> bool:
    counts = report.get("counts", {})
    return (
        counts.get("new_undocumented", 0) > 0
        or counts.get("newly_unhealthy", 0) > 0
        or counts.get("recovered", 0) > 0
        or counts.get("disappeared", 0) > 0
    )
