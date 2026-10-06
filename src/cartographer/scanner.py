"""Cartographer Scanner — discovers services across every configured host.

Hosts and what to scan on each come from config/hosts.yaml (`scan:` block).
Runs discovery commands via SSH (remote) or locally (current host).
Returns raw scan results as lists of dicts.
"""

import json
import os
import platform
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import load_hosts


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    ssh_alias: str
    is_linux: bool = True
    scan_systemd: bool = True
    scan_docker: bool = True
    scan_launchd: bool = False
    scan_ports: bool = True
    scan_cron: bool = True
    repos_dir: str = ""
    scan_n8n: bool = False


def load_devices() -> list[DeviceConfig]:
    """Build scan targets from the hosts config. Empty ssh = scan locally."""
    devices = []
    for name, cfg in load_hosts().items():
        scan = cfg.get("scan") or {}
        devices.append(
            DeviceConfig(
                name=name,
                ssh_alias=cfg.get("ssh", ""),
                is_linux=scan.get("linux", True),
                scan_systemd=scan.get("systemd", True),
                scan_docker=scan.get("docker", True),
                scan_launchd=scan.get("launchd", False),
                scan_ports=scan.get("ports", True),
                scan_cron=scan.get("cron", True),
                repos_dir=scan.get("repos_dir", ""),
                scan_n8n=scan.get("n8n", False),
            )
        )
    return devices


@dataclass
class ScanResult:
    host: str
    scan_time: str
    services: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: str, ssh_alias: Optional[str] = None, timeout: int = 30) -> tuple[str, str]:
    if ssh_alias and not _is_local(ssh_alias):
        full_cmd = ["ssh", "-o", "ConnectTimeout=10", ssh_alias, cmd]
    else:
        full_cmd = ["bash", "-c", cmd]

    try:
        proc = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", f"timeout after {timeout}s"
    except Exception as e:
        return "", str(e)


def _is_local(ssh_alias: str) -> bool:
    if not ssh_alias:
        return True
    hostname = platform.node().split(".")[0].lower()
    return ssh_alias.lower() == hostname.lower()


def scan_systemd(device: DeviceConfig) -> list[dict]:
    cmd = "systemctl --user list-units --type=service,timer --no-pager --no-legend 2>/dev/null"
    stdout, stderr = _run(cmd, device.ssh_alias)
    if not stdout:
        return []

    services = []
    for line in stdout.splitlines():
        cleaned = line.lstrip("● \t")
        parts = cleaned.split()
        if len(parts) < 4:
            continue
        unit = parts[0].strip()
        if not unit or unit in ("UNIT", "LOAD"):
            continue
        active_state = parts[2]
        sub_state = parts[3]
        description = " ".join(parts[4:]) if len(parts) > 4 else ""

        services.append(
            {
                "name": unit,
                "host": device.name,
                "type": "systemd",
                "state": active_state,
                "sub_state": sub_state,
                "description": description,
            }
        )
    return services


def scan_docker(device: DeviceConfig) -> list[dict]:
    fmt = '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","ports":"{{.Ports}}","state":"{{.State}}"}'
    cmd = f"docker ps -a --format '{fmt}' 2>/dev/null"
    stdout, stderr = _run(cmd, device.ssh_alias)
    if not stdout:
        return []

    services = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        port = _extract_primary_port(data.get("ports", ""))
        svc = {
            "name": data["name"],
            "host": device.name,
            "type": "docker",
            "state": data.get("state", "unknown"),
            "image": data.get("image", ""),
            "status": data.get("status", ""),
        }
        if port:
            svc["port"] = port
        services.append(svc)
    return services


def _extract_primary_port(ports_str: str) -> Optional[int]:
    if not ports_str:
        return None
    import re

    matches = re.findall(r"(?:0\.0\.0\.0|[\d.]+):(\d+)->(\d+)", ports_str)
    if matches:
        return int(matches[0][1])
    matches = re.findall(r"(\d+)/tcp", ports_str)
    if matches:
        return int(matches[0])
    return None


def scan_launchd(device: DeviceConfig) -> list[dict]:
    cmd = "launchctl list 2>/dev/null"
    stdout, _ = _run(cmd, device.ssh_alias)
    if not stdout:
        return []

    plist_cmd = 'ls "$HOME"/Library/LaunchAgents/*.plist 2>/dev/null'
    plist_stdout, _ = _run(plist_cmd, device.ssh_alias)
    plist_labels = set()
    for line in (plist_stdout or "").splitlines():
        basename = os.path.basename(line).replace(".plist", "")
        plist_labels.add(basename)

    services = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid_str, status_str, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if label == "PID" or label.startswith("-"):
            continue

        if label not in plist_labels:
            continue

        is_running = pid_str != "-" and pid_str != "0"
        exit_code = int(status_str) if status_str.lstrip("-").isdigit() else 0

        services.append(
            {
                "name": label,
                "host": device.name,
                "type": "launchd",
                "state": "running" if is_running else ("failed" if exit_code != 0 else "stopped"),
                "pid": int(pid_str) if pid_str.isdigit() else None,
                "exit_code": exit_code,
            }
        )

    for label in plist_labels:
        if not any(s["name"] == label for s in services):
            services.append(
                {
                    "name": label,
                    "host": device.name,
                    "type": "launchd",
                    "state": "unloaded",
                    "pid": None,
                    "exit_code": None,
                }
            )

    return services


def scan_ports_linux(device: DeviceConfig) -> list[dict]:
    cmd = "ss -tlnp 2>/dev/null"
    stdout, _ = _run(cmd, device.ssh_alias)
    if not stdout:
        return []

    services = []
    for line in stdout.splitlines():
        if not line.startswith("LISTEN"):
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[3]
        port = _parse_port(local_addr)
        if port is None:
            continue

        process = ""
        if len(parts) >= 6:
            import re

            m = re.search(r'users:\(\("([^"]+)"', parts[5])
            if m:
                process = m.group(1)

        bind_addr = local_addr.rsplit(":", 1)[0] if ":" in local_addr else "*"

        services.append(
            {
                "name": f"port:{port}",
                "host": device.name,
                "type": "port",
                "port": port,
                "state": "listening",
                "process": process,
                "bind": bind_addr,
            }
        )
    return services


def scan_ports_macos(device: DeviceConfig) -> list[dict]:
    cmd = "lsof -i -P -n 2>/dev/null | grep LISTEN"
    stdout, _ = _run(cmd, device.ssh_alias)
    if not stdout:
        return []

    seen = set()
    services = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        process = parts[0]
        addr_part = parts[8]
        port = _parse_port(addr_part)
        if port is None:
            continue

        key = (process, port)
        if key in seen:
            continue
        seen.add(key)

        bind_addr = addr_part.rsplit(":", 1)[0] if ":" in addr_part else "*"

        services.append(
            {
                "name": f"port:{port}",
                "host": device.name,
                "type": "port",
                "port": port,
                "state": "listening",
                "process": process,
                "bind": bind_addr,
            }
        )
    return services


def _parse_port(addr: str) -> Optional[int]:
    if not addr:
        return None
    try:
        port_str = addr.rsplit(":", 1)[-1]
        return int(port_str)
    except (ValueError, IndexError):
        return None


def scan_cron(device: DeviceConfig) -> list[dict]:
    cmd = "crontab -l 2>/dev/null"
    stdout, _ = _run(cmd, device.ssh_alias)
    if not stdout or "no crontab" in stdout.lower():
        return []

    services = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        services.append(
            {
                "name": f"cron:{line[:60]}",
                "host": device.name,
                "type": "cron",
                "state": "scheduled",
                "schedule": line,
            }
        )
    return services


def scan_repos(device: DeviceConfig) -> list[dict]:
    cmd = f"/usr/bin/find {device.repos_dir} -maxdepth 2 -name .git -type d 2>/dev/null"
    stdout, _ = _run(cmd, device.ssh_alias, timeout=15)
    if not stdout:
        return []

    repos = []
    for line in stdout.splitlines():
        repo_dir = line.replace("/.git", "")
        repo_name = os.path.basename(repo_dir)
        repos.append(
            {
                "name": f"repo:{repo_name}",
                "host": device.name,
                "type": "repo",
                "state": "present",
                "path": repo_dir,
            }
        )
    return repos


def scan_n8n(device: DeviceConfig) -> list[dict]:
    cmd = (
        "docker exec n8n sh -c 'cat \"$HOME/.n8n/database.sqlite\"' > /tmp/n8n_carto.sqlite 2>/dev/null && "
        "sqlite3 -json /tmp/n8n_carto.sqlite "
        "'SELECT id, name, active FROM workflow_entity ORDER BY name' 2>/dev/null"
    )
    stdout, stderr = _run(cmd, device.ssh_alias, timeout=15)
    if not stdout:
        cmd_fallback = (
            "docker exec n8n sh -c 'cat \"$HOME/.n8n/database.sqlite\"' > /tmp/n8n_carto.sqlite 2>/dev/null && "
            "sqlite3 /tmp/n8n_carto.sqlite "
            "\"SELECT id || '|' || name || '|' || active FROM workflow_entity ORDER BY name\" 2>/dev/null"
        )
        stdout, _ = _run(cmd_fallback, device.ssh_alias, timeout=15)
        if not stdout:
            return []

        workflows = []
        for line in stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            wf_id, name, active = parts[0], parts[1], parts[2]
            workflows.append(
                {
                    "name": f"n8n:{name}",
                    "host": device.name,
                    "type": "n8n",
                    "state": "active" if active == "1" else "inactive",
                    "workflow_id": wf_id,
                }
            )
        return workflows

    try:
        rows = json.loads(stdout)
    except json.JSONDecodeError:
        return []

    workflows = []
    for row in rows:
        name = row.get("name", "unknown")
        active = row.get("active", 0)
        wf_id = row.get("id", "")
        workflows.append(
            {
                "name": f"n8n:{name}",
                "host": device.name,
                "type": "n8n",
                "state": "active" if active else "inactive",
                "workflow_id": wf_id,
            }
        )
    return workflows


def scan_device(device: DeviceConfig) -> ScanResult:
    now = datetime.now(timezone.utc).isoformat()
    result = ScanResult(host=device.name, scan_time=now)

    scanners = []
    if device.scan_systemd:
        scanners.append(("systemd", scan_systemd))
    if device.scan_docker:
        scanners.append(("docker", scan_docker))
    if device.scan_launchd:
        scanners.append(("launchd", scan_launchd))
    if device.scan_ports:
        if device.is_linux:
            scanners.append(("ports", scan_ports_linux))
        else:
            scanners.append(("ports", scan_ports_macos))
    if device.scan_cron:
        scanners.append(("cron", scan_cron))
    if device.repos_dir:
        scanners.append(("repos", scan_repos))
    if device.scan_n8n:
        scanners.append(("n8n", scan_n8n))

    for scan_name, scanner_fn in scanners:
        try:
            services = scanner_fn(device)
            result.services.extend(services)
        except Exception as e:
            result.errors.append(f"{scan_name}: {e}")

    return result


def scan_all() -> list[ScanResult]:
    results = []
    for device in load_devices():
        result = scan_device(device)
        results.append(result)
    return results
