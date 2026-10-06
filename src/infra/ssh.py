from __future__ import annotations

import logging
import os
import shlex
import subprocess

log = logging.getLogger("ssh")

DEFAULT_HOST = os.environ.get("ODIN_SSH_HOST", "laptop")


def run_cmd(
    cmd_parts: list[str],
    host: str = DEFAULT_HOST,
    timeout: int = 15,
    connect_timeout: int = 10,
) -> subprocess.CompletedProcess:
    remote_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    return subprocess.run(
        ["ssh", f"-o ConnectTimeout={connect_timeout}", "-o BatchMode=yes", host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_reachable(host: str = DEFAULT_HOST, timeout: int = 10) -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-o ConnectTimeout=5", "-o BatchMode=yes", host, "true"],
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except Exception as e:
        log.debug(f"SSH reachability check failed for {host}: {e}")
        return False


def write_file(host: str, remote_path: str, content: str, timeout: int = 15) -> bool:
    if remote_path.startswith("~/"):
        safe_path = "~/" + shlex.quote(remote_path[2:])
    else:
        safe_path = shlex.quote(remote_path)
    try:
        result = subprocess.run(
            ["ssh", "-o ConnectTimeout=10", "-o BatchMode=yes", host, f"cat > {safe_path}"],
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except Exception as e:
        log.error(f"Failed to write {remote_path} on {host}: {e}")
        return False
