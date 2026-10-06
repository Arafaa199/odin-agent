#!/usr/bin/env python3
"""Monitor a Mac's LaunchAgents and alert (via the Odin shim) on CONSECUTIVE failures.

Policy: a label must fail on THRESHOLD consecutive runs before it pages. A single or
duplicate transient failure is logged only. This replaces the old page-on-first-transition
behaviour, which fired on every transient restart.

Counting is done PER RUN, not per monitor-observation — `launchctl list`'s last-exit
column persists between an agent's scheduled runs, so observing it 3× is NOT 3 failed
runs (e.g. a nightly job that failed once would otherwise page all day). We read the
`runs` counter from `launchctl print` and only advance the consecutive count when the
run counter actually increments. A counter that moves BACKWARDS means launchd reset it
(reboot / bootout+bootstrap), so the stored baseline is re-seeded rather than held —
otherwise a stuck counter keeps paging a healthy job forever.

Signals (all gated by the same THRESHOLD-consecutive counter):
  - KeepAlive agent down with a nonzero exit, or absent from launchctl (continuous
    condition -> counted per observation).
  - Interval/scheduled agent whose *new* run exited nonzero (counted per run). Catches
    e.g. an hourly ETL heartbeat that exits 2 whenever its upstream is down.
  - A curated agent that has silently dropped out of launchctl (unloaded).

State: ~/.local/state/launchd-monitor-state.json  {label: {cons, alerted, last_runs}}
       (override with $LAUNCHD_MONITOR_STATE; set $LAUNCHD_MONITOR_DRYRUN=1 to print
       instead of POSTing).
Labels: $LAUNCHD_MONITOR_KEEPALIVE / $LAUNCHD_MONITOR_INTERVAL (comma-separated)
        override the example sets below.
Runs from its own LaunchAgent (StartInterval lives in the plist). Stdlib only, so
it does not need the project's virtualenv.
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

STATE_PATH = Path(
    os.environ.get("LAUNCHD_MONITOR_STATE", "~/.local/state/launchd-monitor-state.json")
).expanduser()
SHIM_URL = os.environ.get("ODIN_SHIM_URL", "http://localhost:3340/send")
THRESHOLD = 3
DRY_RUN = os.environ.get("LAUNCHD_MONITOR_DRYRUN") == "1"
UID = os.getuid()


def _labels(env_var: str, default: set[str]) -> frozenset:
    raw = os.environ.get(env_var, "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip()) if raw else frozenset(default)


# KeepAlive agents that must always be loaded AND running.
KEEPALIVE_AGENTS = _labels(
    "LAUNCHD_MONITOR_KEEPALIVE",
    {"com.example.odin-node", "com.example.imessage-bridge", "com.example.apple-shim"},
)

# Interval/scheduled agents that must stay LOADED; their runs are exit-code watched.
INTERVAL_AGENTS = _labels(
    "LAUNCHD_MONITOR_INTERVAL",
    {
        "com.example.transaction-import",
        "com.example.mail-intel",
        "com.example.receipt-ingest",
        "com.example.docs-assembler",
        "com.example.etl-heartbeat",
    },
)

# Agents whose nonzero exit is benign-by-design — exit failures are NOT counted (only
# unloaded / down is). Add a label here only after confirming its nonzero exit is benign.
EXCLUDE_NONZERO: frozenset = frozenset()

_STATE_RE = re.compile(r"^\s*state = (.+)$", re.M)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)", re.M)
_EXIT_RE = re.compile(r"^\s*last exit code = (-?\d+)", re.M)


def probe_label(label: str) -> dict:
    """`launchctl print` a label -> {loaded, running, runs, last_exit}."""
    result = subprocess.run(
        ["launchctl", "print", f"gui/{UID}/{label}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return {"loaded": False, "running": False, "runs": None, "last_exit": None}
    out = result.stdout
    m = _STATE_RE.search(out)
    running = bool(m and m.group(1).strip() == "running")
    m = _RUNS_RE.search(out)
    runs = int(m.group(1)) if m else None
    m = _EXIT_RE.search(out)
    last_exit = int(m.group(1)) if m else 0
    return {"loaded": True, "running": running, "runs": runs, "last_exit": last_exit}


def next_count(label: str, obs: dict, prev: dict) -> tuple[int, object]:
    """Consecutive-failure count for one label. Returns (cons, last_runs).

    KeepAlive: down-state counted per observation.
    Interval: exit failures counted per RUN (advance only when `runs` increments);
              being unloaded counted per observation.
    """
    pcons = int(prev.get("cons", 0))
    plast = prev.get("last_runs")

    if label in KEEPALIVE_AGENTS:
        down = (not obs["loaded"]) or (not obs["running"] and (obs["last_exit"] or 0) != 0)
        return (pcons + 1 if down else 0), obs.get("runs")

    # interval / scheduled
    if not obs["loaded"]:
        return pcons + 1, plast  # unloaded is a continuous condition
    if label in EXCLUDE_NONZERO:
        return 0, obs.get("runs")
    runs = obs.get("runs")
    failed = (obs.get("last_exit") or 0) != 0
    if plast is None:  # first sight: seed from the latest run result
        return (1 if failed else 0), runs
    if runs is not None and isinstance(plast, int) and runs < plast:
        # launchd's per-label run counter went BACKWARDS -> it was reset (reboot, or the
        # label was booted out and back in). The stored baseline is meaningless now, and
        # `runs > plast` would stay false forever, freezing cons/alerted on a healthy job
        # (three labels once sat stuck `alerted:true` for five days after a reboot).
        # Re-baseline off the new counter; a healthy job lands on cons=0, which clears
        # `alerted` in evaluate().
        return (1 if failed else 0), runs
    if runs is not None and runs > plast:  # a genuinely new run happened
        return ((pcons + 1) if failed else 0), runs
    return pcons, plast  # no new run -> hold the counter


def evaluate(observations: dict, prev_state: dict, threshold: int = THRESHOLD):
    """Pure decision core — no I/O. Returns (failures, recoveries, transients, new_state).

    failures   [(label, last_exit, cons)]  crossed the threshold this run -> page once
    recoveries [label]                      was alerting, now clean
    transients [(label, cons)]              failing but below threshold -> log only
    """
    failures, recoveries, transients, new_state = [], [], [], {}
    for label in sorted(observations):
        obs = observations[label]
        prev = prev_state.get(label, {"cons": 0, "alerted": False, "last_runs": None})
        cons, last_runs = next_count(label, obs, prev)
        alerted = bool(prev.get("alerted", False))
        if cons == 0:
            if alerted:
                recoveries.append(label)
            new_state[label] = {"cons": 0, "alerted": False, "last_runs": last_runs}
        elif cons >= threshold and not alerted:
            failures.append((label, obs.get("last_exit"), cons))
            new_state[label] = {"cons": cons, "alerted": True, "last_runs": last_runs}
        else:
            if not alerted:
                transients.append((label, cons))
            new_state[label] = {"cons": cons, "alerted": alerted, "last_runs": last_runs}
    return failures, recoveries, transients, new_state


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def render_message(failures: list, recoveries: list) -> str:
    ts = datetime.now().strftime("%H:%M")
    lines = [f"*LaunchAgent Monitor* — {os.uname().nodename} {ts}"]
    if failures:
        lines.append("")
        for label, last_exit, cons in failures:
            lines.append(f"❌ `{label}` failing (exit {last_exit}, {cons} consecutive runs)")
        lines.append("\nCheck: `launchctl print gui/$(id -u)/<label>`")
    if recoveries:
        lines.append("")
        for label in recoveries:
            lines.append(f"✅ `{label}` recovered")
    return "\n".join(lines)


def _incident_muted() -> bool:
    """Honor the incident mute switch (~/.odin/mute-until.json, see src/comms/mute.py).

    Inline (no src.comms import) — this monitor runs standalone under launchd.
    TTL is enforced on read (cap 24h); malformed/expired = not muted.
    """
    import json as _json, time as _time
    from pathlib import Path as _Path

    try:
        d = _json.loads((_Path.home() / ".odin" / "mute-until.json").read_text())
        until = float(d.get("until", 0))
        now = _time.time()
        return now < until <= now + 24 * 3600
    except (OSError, ValueError, TypeError):
        return False


def send_alert(message: str) -> bool:
    if _incident_muted():
        print("[MUTED] launchd alert suppressed")
        return True
    if DRY_RUN:
        print("[DRY-RUN] would POST to shim:\n" + message)
        return True
    try:
        data = json.dumps({"message": message}).encode()
        req = urllib.request.Request(
            SHIM_URL, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def main() -> int:
    monitored = sorted(KEEPALIVE_AGENTS | INTERVAL_AGENTS)
    observations = {label: probe_label(label) for label in monitored}
    prev_state = load_state()
    failures, recoveries, transients, new_state = evaluate(observations, prev_state)
    save_state(new_state)

    for label, cons in transients:
        print(f"transient: {label} failing {cons}/{THRESHOLD} runs (logged, not paged)")

    if not failures and not recoveries:
        print(f"clean: {len(monitored)} agents watched, no consecutive failures")
        return 0

    sent = send_alert(render_message(failures, recoveries))
    print(
        f"{'sent' if sent else 'FAILED'}: {len(failures)} failures, "
        f"{len(recoveries)} recoveries, {len(transients)} transients"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
