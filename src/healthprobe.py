"""Infrastructure Health Probe — config-driven service monitoring.

Checks every host in config/hosts.yaml (or hosts.example.yaml): reachability,
systemd units, Docker containers, launchd agents, HTTP endpoints, proxied
domains, databases, backups, disk and memory. Writes health.json + failed.log
and notifies via Odin comms on NEW failures and recoveries only.

Runs every 15 min from a timer on the worker host.

Usage: python -m src.healthprobe [--json] [--quiet]
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from . import comms
from .config import load_hosts, load_settings, load_yaml, config_path

log = logging.getLogger("healthprobe")

STATE_DIR = Path(os.environ.get("HEALTHPROBE_STATE_DIR", "~/.odin/.healthprobe")).expanduser()
HEALTH_JSON = STATE_DIR / "health.json"
FAILED_LOG = STATE_DIR / "failed.log"
PREV_FAILURES_PATH = STATE_DIR / "prev_failures.json"
ALERT_COOLDOWN_PATH = STATE_DIR / "alert_cooldowns.json"

# Hours before the same check can re-alert after flapping (fail→ok→fail)
ALERT_COOLDOWN_HOURS = 1

DISK_WARN_PERCENT = 80
DISK_CRIT_PERCENT = 90
MEMORY_WARN_PERCENT = 90
SSH_TIMEOUT = 10
HTTP_TIMEOUT = 8


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class Check:
    name: str
    device: str
    category: str
    status: Status = Status.OK
    message: str = ""
    latency_ms: Optional[float] = None


def _probe_config() -> dict:
    data = load_yaml(config_path("hosts", "ODIN_HOSTS"))
    return data if isinstance(data, dict) else {}


def _run_ssh(host: str, cmd: str, timeout: int = SSH_TIMEOUT, port: int | None = None):
    """Run command on remote host via SSH. Returns (exit_code, stdout+stderr)."""
    argv = ["ssh", "-o", "ConnectTimeout={}".format(timeout), "-o", "StrictHostKeyChecking=no"]
    if port:
        argv += ["-p", str(port)]
    try:
        r = subprocess.run(
            [*argv, host, cmd], capture_output=True, text=True, timeout=timeout + 5
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "SSH timeout"
    except Exception as e:
        return -1, str(e)


def _run_local(cmd: str, timeout: int = 10) -> tuple[int, str]:
    """Run command locally."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


def _run_on(cfg: dict, cmd: str, timeout: int = SSH_TIMEOUT) -> tuple[int, str]:
    """Run on the host described by cfg: locally when ssh is empty."""
    ssh = cfg.get("ssh", "")
    if not ssh:
        return _run_local(cmd, timeout=timeout)
    return _run_ssh(ssh, cmd, timeout=timeout, port=cfg.get("ssh_port"))


def _http_code(out: str) -> int:
    try:
        return int(out.strip()[-3:]) if out.strip() else 0
    except (ValueError, IndexError):
        return 0


def _http_check(name: str, cfg: dict, spec: dict) -> Check:
    """HTTP endpoint check, curled from the host itself."""
    port = spec["port"]
    path = spec.get("path", "/")
    expected = tuple(spec.get("expect", [200]))
    url = "{}://{}:{}/{}".format(
        spec.get("scheme", "http"), spec.get("target", "localhost"), port, path.lstrip("/")
    )
    check_name = "{}:{}{}".format(name, port, path)
    cmd = 'curl -s -o /dev/null -w "%{{http_code}}" --connect-timeout {} {}'.format(
        HTTP_TIMEOUT, url
    )
    start = time.monotonic()
    code, out = _run_on(cfg, cmd, timeout=HTTP_TIMEOUT + 5)
    elapsed = (time.monotonic() - start) * 1000
    http_code = _http_code(out)

    if code == -1 or http_code == 0:
        status, msg = Status.FAIL, "unreachable: {}".format(out[:100])
    elif http_code in expected:
        status, msg = Status.OK, "HTTP {}".format(http_code)
    else:
        status, msg = Status.FAIL, "HTTP {} (expected {})".format(http_code, expected)
    return Check(check_name, name, "endpoint", status, msg, elapsed)


def _check_outcome_probe(probe: dict) -> Check:
    """POST a real request; anything but a 200 inside the deadline is a FAIL.

    A /health endpoint answers "am I up". This asks what a caller actually
    needs, "can you answer me": a recall service once hung for hours serving
    nothing while its /health stayed green.
    """
    name = probe.get("name", probe["url"])
    device = probe.get("device", "local")
    timeout = probe.get("timeout", 10)
    key_env = probe.get("key_env", "")
    headers = {"Content-Type": "application/json"}
    if key_env:
        key = os.environ.get(key_env, "")
        if not key:
            return Check(name, device, "endpoint", Status.FAIL, f"{key_env} absent from environment")
        headers[probe.get("key_header", "Authorization")] = key

    body = json.dumps(probe.get("body", {})).encode()
    req = urllib.request.Request(probe["url"], data=body, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            http_code = resp.status
    except urllib.error.HTTPError as e:
        http_code = e.code
    except Exception as e:
        elapsed = (time.monotonic() - start) * 1000
        msg = "no answer within {}s ({})".format(timeout, type(e).__name__)
        return Check(name, device, "endpoint", Status.FAIL, msg, elapsed)
    elapsed = (time.monotonic() - start) * 1000
    if http_code == 200:
        return Check(name, device, "endpoint", Status.OK, "200 in {:.0f}ms".format(elapsed), elapsed)
    return Check(name, device, "endpoint", Status.FAIL, "HTTP {}".format(http_code), elapsed)


def _check_reachability(hosts: dict[str, dict]) -> tuple[list[Check], set[str]]:
    """Reachability for every remote host with an address.

    Uses `tailscale status` when available, else a single ping. Hosts marked
    intermittent (laptops) get SKIP instead of FAIL. Returns (checks,
    offline_intermittent).
    """
    checks: list[Check] = []
    offline_intermittent: set[str] = set()
    targets = {n: c for n, c in hosts.items() if c.get("ssh") and c.get("address")}
    ts_code, ts_out = (
        _run_local("tailscale status 2>&1") if shutil.which("tailscale") else (1, "")
    )

    for name, cfg in targets.items():
        ip = cfg["address"]
        if ts_code == 0:
            line = [ln for ln in ts_out.split("\n") if ip in ln]
            online = bool(line) and "offline" not in line[0].lower()
        else:
            ping_code, _ = _run_local("ping -c 1 -W 3 {} 2>/dev/null".format(ip), timeout=5)
            online = ping_code == 0

        check_name = "reach/{}".format(name)
        if online:
            checks.append(Check(check_name, name, "network", Status.OK, "reachable"))
        elif cfg.get("intermittent"):
            offline_intermittent.add(name)
            checks.append(
                Check(check_name, name, "network", Status.SKIP, "offline (intermittent device)")
            )
        else:
            checks.append(Check(check_name, name, "network", Status.FAIL, "unreachable"))
    return checks, offline_intermittent


def _check_device_resources(name: str, cfg: dict) -> list[Check]:
    """Check disk and memory on a device."""
    checks = []
    code, out = _run_on(cfg, "df / 2>/dev/null | tail -1")
    if code == 0 and out:
        try:
            pct = int([p for p in out.split() if "%" in p][0].rstrip("%"))
            status = Status.OK
            if pct >= DISK_CRIT_PERCENT:
                status = Status.FAIL
            elif pct >= DISK_WARN_PERCENT:
                status = Status.WARN
            checks.append(Check("disk/{}".format(name), name, "resource", status, f"{pct}% used"))
        except (IndexError, ValueError):
            pass

    code, out = _run_on(cfg, "free 2>/dev/null | grep Mem")
    if code == 0 and "Mem:" in out:
        parts = out.split()
        try:
            total, used = int(parts[1]), int(parts[2])
            pct = int(used * 100 / total) if total > 0 else 0
            status = Status.OK if pct < MEMORY_WARN_PERCENT else Status.WARN
            checks.append(Check("memory/{}".format(name), name, "resource", status, f"{pct}% used"))
        except (IndexError, ValueError):
            pass
    return checks


def _systemd_status(svc: str, state: str, critical: bool, on_demand: bool) -> tuple[Status, str]:
    if state == "active":
        return Status.OK, state
    if state == "inactive":
        # A scheduled timer reports `active`; `inactive` on a timer means it
        # was stopped. Critical timers escalate to FAIL so Odin comms pages.
        if svc.endswith(".timer"):
            if critical:
                return Status.FAIL, "inactive (critical timer stopped)"
            return Status.WARN, "inactive (timer stopped)"
        if on_demand:
            return Status.OK, state
        return Status.WARN, "inactive (not running)"
    if on_demand:
        # Event-driven one-shots: their unit state is the residue of the LAST
        # manual run, not a statement about now. A days-old `failed` once pinned
        # the whole estate `degraded` while nothing was wrong. Visible, never red.
        return Status.WARN, "{} (on-demand one-shot: stale run state)".format(state)
    return Status.FAIL, state


def _check_systemd_services(name: str, cfg: dict) -> list[Check]:
    """Check systemd user units on a host."""
    services = cfg.get("systemd", [])
    critical = set(cfg.get("critical_timers", []))
    on_demand = set(cfg.get("on_demand", []))
    code, out = _run_on(cfg, "systemctl --user is-active {} 2>/dev/null".format(" ".join(services)))
    if code == -1:
        return [Check("systemd/{}".format(name), name, "service", Status.FAIL, "SSH unreachable")]

    statuses = out.strip().split("\n")
    checks = []
    for i, svc in enumerate(services):
        state = statuses[i].strip() if i < len(statuses) else "unknown"
        status, msg = _systemd_status(svc, state, svc in critical, svc in on_demand)
        checks.append(Check("systemd/{}/{}".format(name, svc), name, "service", status, msg))
    return checks


def _check_docker_containers(name: str, cfg: dict) -> list[Check]:
    """Check that the listed Docker containers exist and are Up."""
    code, out = _run_on(cfg, "docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null")
    if code == -1:
        return [Check("docker/{}".format(name), name, "container", Status.FAIL, "SSH unreachable")]

    running = {}
    for line in out.split("\n"):
        if "|" in line:
            cname, status = line.split("|", 1)
            running[cname.strip()] = status.strip()

    checks = []
    for container in cfg.get("docker", []):
        check_name = "docker/{}/{}".format(name, container)
        st = running.get(container)
        if st is None:
            checks.append(Check(check_name, name, "container", Status.FAIL, "not found"))
        else:
            status = Status.OK if st.startswith("Up") else Status.FAIL
            checks.append(Check(check_name, name, "container", status, st))
    return checks


def _check_launchd_agents(name: str, cfg: dict) -> list[Check]:
    """Check launchd agents on a macOS host."""
    code, out = _run_on(cfg, "launchctl list 2>/dev/null")
    if code == -1:
        return [Check("launchd/{}".format(name), name, "service", Status.SKIP, "unreachable")]

    loaded = {}
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 3:
            loaded[parts[2].strip()] = (parts[0].strip(), parts[1].strip())

    checks = []
    for agent in cfg.get("launchd", []):
        check_name = "launchd/{}".format(agent)
        if agent not in loaded:
            checks.append(Check(check_name, name, "service", Status.FAIL, "not loaded"))
            continue
        pid, exit_code = loaded[agent]
        if pid == "-" and exit_code == "0":
            # Not running but exited cleanly (normal for on-demand agents)
            checks.append(Check(check_name, name, "service", Status.OK, "idle (exit 0)"))
        elif pid != "-":
            checks.append(Check(check_name, name, "service", Status.OK, f"running (pid {pid})"))
        else:
            status = Status.WARN if exit_code == "1" else Status.FAIL
            checks.append(Check(check_name, name, "service", status, f"exit code {exit_code}"))
    return checks


def _check_postgres(name: str, cfg: dict) -> Check:
    """SELECT 1 inside the Postgres container, using the container's own env."""
    container = cfg["postgres_container"]
    cmd = (
        "docker exec {} sh -c 'PGPASSWORD=\"$POSTGRES_PASSWORD\" psql "
        "-U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -c \"SELECT 1\"' 2>&1"
    ).format(container)
    code, out = _run_on(cfg, cmd)
    ok = code == 0 and "1 row" in out
    return Check(
        "postgres/{}".format(name), name, "database",
        Status.OK if ok else Status.FAIL, "accepting queries" if ok else out[:120],
    )


def _check_redis(name: str, cfg: dict) -> Check:
    code, out = _run_on(cfg, "docker exec {} redis-cli ping 2>&1".format(cfg["redis_container"]))
    ok = code == 0 and "PONG" in out
    return Check(
        "redis/{}".format(name), name, "database", Status.OK if ok else Status.FAIL,
        "PONG" if ok else out[:120],
    )


def _check_dns(name: str, cfg: dict) -> Check:
    """Check the host's local resolver answers."""
    code, out = _run_on(cfg, "dig +short example.com @127.0.0.1 2>&1 | head -1")
    ok = code == 0 and bool(out) and not out.startswith(";;")
    return Check(
        "dns/{}".format(name), name, "network", Status.OK if ok else Status.FAIL,
        "resolving" if ok else out[:120],
    )


def _check_backup_dir(name: str, cfg: dict) -> Check:
    """Check the newest file in backup_dir is younger than backup_max_age_hours."""
    backup_dir = cfg["backup_dir"].rstrip("/")
    max_age = cfg.get("backup_max_age_hours", 26)
    cmd = 'stat -c "%Y" {d}/$(ls -t {d}/ 2>/dev/null | head -1) 2>/dev/null'.format(d=backup_dir)
    code, out = _run_on(cfg, cmd)
    check_name = "backup/{}".format(name)
    if code == 0 and out.strip().isdigit():
        age_hours = (time.time() - int(out.strip())) / 3600
        if age_hours < max_age:
            return Check(check_name, name, "backup", Status.OK, "{:.0f}h old".format(age_hours))
        msg = "{:.0f}h old (> {}h)".format(age_hours, max_age)
        return Check(check_name, name, "backup", Status.WARN, msg)
    return Check(check_name, name, "backup", Status.FAIL, "no backup found")


def _check_proxy(name: str, cfg: dict) -> list[Check]:
    """Proxy container running, then each domain it fronts.

    The container check is the root cause for the domain checks (see
    _correlate_root_causes), so a dead proxy pages once, not once per domain.
    """
    proxy = cfg["proxy"]
    container = proxy.get("container", "caddy")
    _, out = _run_on(cfg, "docker inspect -f '{{{{.State.Running}}}}' {} 2>&1".format(container))
    running = out.strip() == "true"
    checks = [
        Check("proxy/{}".format(name), name, "service",
              Status.OK if running else Status.FAIL, out.strip()[:60])
    ]
    for domain, codes in (proxy.get("domains") or {}).items():
        cmd = 'curl -sk -o /dev/null -w "%{{http_code}}" --connect-timeout {} https://{}'.format(
            HTTP_TIMEOUT, domain
        )
        _, d_out = _run_on(cfg, cmd)
        http_code = _http_code(d_out)
        checks.append(
            Check(
                "domain/{}/{}".format(name, domain), name, "endpoint",
                Status.OK if http_code in codes else Status.FAIL,
                "HTTP {}".format(http_code) if http_code else "unreachable",
            )
        )
    return checks


def _check_bridge_session(name: str, cfg: dict, spec: dict) -> Check:
    """Outcome-level session check for a chat bridge.

    Process health is not connection health: a bridge unit once ran "active"
    for months after its session silently logged out. Severity is conditional:
      - connection == "open"                                  -> OK
      - not open AND last message within alive_days           -> FAIL (pages)
      - not open AND no recent traffic (known dead, needs a manual re-pair) -> WARN
    This arms itself: once messages flow again, any later drop pages.
    """
    alive_days = spec.get("alive_days", 7)
    code, out = _run_on(cfg, "curl -s --connect-timeout {} {}".format(HTTP_TIMEOUT, spec["url"]))
    conn, last = "", None
    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            conn, last = data.get("connection", ""), data.get("last_message_at")
        except ValueError:
            pass

    age_days = None
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            age_days = None

    check_name = "{}/{}-session".format(name, spec.get("name", "bridge"))
    if conn == "open":
        return Check(check_name, name, "endpoint", Status.OK, "connection=open")
    if age_days is not None and age_days <= alive_days:
        msg = "connection={} but last msg {:.1f}d ago (<{}d) — live session dropped".format(
            conn or "unreachable", age_days, alive_days
        )
        return Check(check_name, name, "endpoint", Status.FAIL, msg)
    msg = "connection={} — known dead state, no recent traffic, no page".format(
        conn or "unreachable"
    )
    return Check(check_name, name, "endpoint", Status.WARN, msg)


def _check_listening_port(name: str, cfg: dict, port: int) -> Check:
    cmd = "ss -tlnp 2>/dev/null | grep :{p} || lsof -i :{p} 2>/dev/null | grep LISTEN".format(p=port)
    code, out = _run_on(cfg, cmd)
    ok = code == 0 and bool(out.strip())
    return Check(
        "{}:{}/listen".format(name, port), name, "endpoint",
        Status.OK if ok else Status.FAIL, "listening" if ok else "not listening",
    )


def _check_process(name: str, cfg: dict, process: str) -> Check:
    pattern = "[{}]{}".format(process[0], process[1:])
    code, out = _run_on(cfg, "ps aux 2>/dev/null | grep -c '{}'".format(pattern))
    ok = code == 0 and out.strip() not in ("0", "")
    return Check(
        "process/{}/{}".format(name, process), name, "service",
        Status.OK if ok else Status.FAIL, "running" if ok else "not running",
    )


def _check_host(name: str, cfg: dict) -> list[Check]:
    """Run every check configured for one host."""
    checks: list[Check] = []
    if cfg.get("dns"):
        checks.append(_check_dns(name, cfg))
    if cfg.get("postgres_container"):
        checks.append(_check_postgres(name, cfg))
    if cfg.get("redis_container"):
        checks.append(_check_redis(name, cfg))
    if cfg.get("backup_dir"):
        checks.append(_check_backup_dir(name, cfg))
    if cfg.get("docker"):
        checks.extend(_check_docker_containers(name, cfg))
    if cfg.get("systemd"):
        checks.extend(_check_systemd_services(name, cfg))
    if cfg.get("launchd"):
        checks.extend(_check_launchd_agents(name, cfg))
    if cfg.get("proxy"):
        checks.extend(_check_proxy(name, cfg))
    for spec in cfg.get("http", []):
        checks.append(_http_check(name, cfg, spec))
    for spec in cfg.get("bridge_sessions", []):
        checks.append(_check_bridge_session(name, cfg, spec))
    for port in cfg.get("listening_ports", []):
        checks.append(_check_listening_port(name, cfg, port))
    for process in cfg.get("processes", []):
        checks.append(_check_process(name, cfg, process))
    if cfg.get("resources"):
        checks.extend(_check_device_resources(name, cfg))
    return checks


def run_all_checks() -> dict:
    """Run all health checks. Returns structured results."""
    hosts = load_hosts()
    probe_cfg = _probe_config()
    ts = datetime.now(timezone.utc).isoformat()

    log.info("Checking reachability...")
    all_checks, offline_intermittent = _check_reachability(hosts)
    if offline_intermittent:
        log.info(
            "Intermittent devices offline (skipping checks): %s",
            ", ".join(sorted(offline_intermittent)),
        )

    for name, cfg in hosts.items():
        if name in offline_intermittent:
            log.info("Skipping %s (offline)", name)
            continue
        log.info("Checking %s...", name)
        all_checks.extend(_check_host(name, cfg))

    for probe in probe_cfg.get("outcome_probes", []) or []:
        all_checks.append(_check_outcome_probe(probe))

    failures = [c for c in all_checks if c.status == Status.FAIL]
    warnings = [c for c in all_checks if c.status == Status.WARN]
    ok_count = len([c for c in all_checks if c.status == Status.OK])

    return {
        "timestamp": ts,
        "summary": {
            "total": len(all_checks),
            "ok": ok_count,
            "warn": len(warnings),
            "fail": len(failures),
            "status": "healthy"
            if not failures
            else "degraded"
            if len(failures) <= 3
            else "critical",
        },
        "failures": [asdict(c) for c in failures],
        "warnings": [asdict(c) for c in warnings],
        "checks": [asdict(c) for c in all_checks],
        "skipped_devices": sorted(offline_intermittent),
    }


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _filter_cooled_down(check_names: set[str], cooldowns: dict[str, float]) -> set[str]:
    """Remove checks that are still in cooldown from the alert set."""
    now = time.time()
    cutoff = ALERT_COOLDOWN_HOURS * 3600
    return {n for n in check_names if now - cooldowns.get(n, 0) > cutoff}


def _append_failed_log(failures: list[dict], ts: str) -> None:
    if not failures:
        return
    lines = ["[{}] {} failures:".format(ts, len(failures))]
    for f in failures:
        lines.append("  {} ({}) — {}".format(f["name"], f["device"], f["message"]))
    lines.append("")
    with open(FAILED_LOG, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def _correlate_root_causes(
    new_failures: set, current_failures: set, failure_records: list
) -> tuple[set, dict]:
    """Collapse symptom checks under an active root cause.

    Roots: a device offline (`reach/<dev>` failing) swallows every other
    failing check on that device; a proxy container (`proxy/<dev>`) swallows
    the `domain/<dev>/*` checks it fronts. Roots are detected from CURRENT
    failures (an ongoing root keeps suppressing symptoms on every run), while
    only roots that are themselves NEW get alerted.

    Returns (kept_new_failures, suppressed: root_name -> set of symptom names).
    Pure function — unit-testable without I/O.
    """
    recs = {f["name"]: f for f in failure_records}
    suppressed: dict[str, set] = {}
    kept = set(new_failures)

    for root in [n for n in current_failures if n.startswith("reach/")]:
        dev = root.split("/", 1)[1]
        symptoms = {n for n in kept if n != root and recs.get(n, {}).get("device") == dev}
        if symptoms:
            suppressed.setdefault(root, set()).update(symptoms)
            kept -= symptoms

    for root in [n for n in current_failures if n.startswith("proxy/")]:
        dev = root.split("/", 1)[1]
        symptoms = {n for n in kept if n.startswith("domain/{}/".format(dev))}
        if symptoms:
            suppressed.setdefault(root, set()).update(symptoms)
            kept -= symptoms

    return kept, suppressed


def _notify_changes(result: dict, skipped_devices: set | None = None) -> None:
    """Notify via Odin comms only on new failures or recoveries."""
    suppressed_checks = set(_probe_config().get("suppressed_checks", []) or [])
    current_failures = {f["name"] for f in result["failures"]}
    prev_failures = set(_load_json(PREV_FAILURES_PATH, []))

    # Drop checks of skipped (intermittent-offline) devices from both sides so
    # lid-close/open cycles don't trigger alerts.
    if skipped_devices:
        prev_failures = {n for n in prev_failures if not any(d in n for d in skipped_devices)}

    new_failures = current_failures - prev_failures - suppressed_checks
    recoveries = prev_failures - current_failures - suppressed_checks
    current_failures -= suppressed_checks

    # Collapse symptoms under an active root cause (host down, proxy down) so
    # one root pages once instead of fanning out N symptom alerts.
    new_failures, suppressed = _correlate_root_causes(
        new_failures, current_failures, result["failures"]
    )

    # Per-check alert cooldown prevents flap spam
    cooldowns = _load_json(ALERT_COOLDOWN_PATH, {})
    new_failures = _filter_cooled_down(new_failures, cooldowns)

    if not new_failures and not recoveries:
        return

    now = time.time()
    for name in new_failures:
        cooldowns[name] = now
    ALERT_COOLDOWN_PATH.write_text(json.dumps(cooldowns))

    lines = ["*Health Probe* — {}".format(datetime.now().strftime("%Y-%m-%d %H:%M"))]
    if new_failures:
        lines += ["", "*NEW FAILURES* ({}):".format(len(new_failures))]
        for f in result["failures"]:
            if f["name"] in new_failures:
                extra = ""
                if f["name"] in suppressed:
                    downs = sorted(suppressed[f["name"]])
                    extra = " [ROOT CAUSE — {} downstream suppressed: {}]".format(
                        len(downs), ", ".join(downs[:6])
                    )
                lines.append("  {} — {}{}".format(f["name"], f["message"], extra))
    if recoveries:
        lines += ["", "*RECOVERED* ({}):".format(len(recoveries))]
        lines += ["  {} — back online".format(name) for name in sorted(recoveries)]

    summary = result["summary"]
    lines += [
        "",
        "_{} checks: {} ok, {} warn, {} fail — {}_".format(
            summary["total"], summary["ok"], summary["warn"], summary["fail"], summary["status"]
        ),
    ]
    msg = "\n".join(lines)

    root_down = any(r in new_failures for r in suppressed)
    is_critical = summary["status"] == "critical" or len(new_failures) >= 3 or root_down
    if is_critical and comms.available():
        comms.send_all(msg)
    elif not comms.send(msg):
        log.warning("All transports unavailable — alert logged only")

    # Suppressed symptoms are NOT persisted as "seen": if they outlive their
    # root cause they must surface as new failures on the next run.
    all_suppressed = set().union(*suppressed.values()) if suppressed else set()
    PREV_FAILURES_PATH.write_text(json.dumps(sorted(current_failures - all_suppressed)))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    json_mode = "--json" in sys.argv
    quiet = "--quiet" in sys.argv

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    comms.init_transports(load_settings())

    log.info("=== Health Probe starting ===")
    result = run_all_checks()

    HEALTH_JSON.write_text(json.dumps(result, indent=2))
    _append_failed_log(result["failures"], result["timestamp"])
    _notify_changes(result, skipped_devices=set(result.get("skipped_devices", [])))

    summary = result["summary"]
    if json_mode:
        print(json.dumps(result, indent=2))
    elif not quiet:
        log.info(
            "=== Results: %s ok, %s warn, %s fail — %s ===",
            summary["ok"],
            summary["warn"],
            summary["fail"],
            summary["status"].upper(),
        )
        for f in result["failures"]:
            log.error("  FAIL: %s (%s) — %s", f["name"], f["device"], f["message"])
        for w in result["warnings"]:
            log.warning("  WARN: %s (%s) — %s", w["name"], w["device"], w["message"])


if __name__ == "__main__":
    main()
