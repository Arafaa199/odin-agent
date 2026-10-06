from __future__ import annotations

import json
import logging
import os
import re
import resource
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("claude_runner")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "opus")

CLAUDE_PATHS = [
    os.path.expanduser("~/.local/share/claude/versions"),
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    "/usr/bin",
]

# Resource limits for Claude subprocess
MAX_MEMORY_BYTES = 4 * 1024**3  # 4 GB virtual memory
MAX_PROCS = 64  # max child processes

# Destructive output patterns to flag
DESTRUCTIVE_OUTPUT_PATTERNS = re.compile(
    r"deleted?\s+\d+|"
    r"rm\s+-rf?\s+/|"
    r"DROP\s+(?:TABLE|DATABASE|SCHEMA)|"
    r"TRUNCATE\s+|"
    r"removed\s+['\"]?/|"
    r"force.?push|"
    r"--hard\b|"
    r"Deleted\s+\d+\s+(?:files?|rows?|records?|entries)|"
    r"wiped?\b|"
    r"destroyed?\b|"
    r"purged?\b",
    re.IGNORECASE,
)


@dataclass
class RunResult:
    exit_code: int
    output: str
    timed_out: bool
    duration_seconds: float
    destructive_warnings: list[str] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0


def _version_key(f: Path) -> tuple:
    try:
        return tuple(int(x) for x in f.name.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _find_claude() -> str:
    for d in CLAUDE_PATHS:
        p = Path(d)
        if p.is_dir():
            versioned = [f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")]
            if versioned:
                best = max(versioned, key=_version_key)
                return str(best)
        claude_bin = p / "claude"
        if claude_bin.exists():
            return str(claude_bin)
    return "claude"


def _set_resource_limits():
    """Called in subprocess via preexec_fn to enforce resource caps."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    except (ValueError, OSError):
        pass  # RLIMIT_AS not available on all platforms
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCS, MAX_PROCS))
    except (ValueError, OSError):
        pass


def _scan_output(output: str) -> list[str]:
    """Scan Claude output for destructive patterns. Returns list of warnings."""
    warnings = []
    for i, line in enumerate(output.splitlines(), 1):
        matches = DESTRUCTIVE_OUTPUT_PATTERNS.findall(line)
        if matches:
            snippet = line.strip()[:120]
            warnings.append(f"L{i}: {snippet}")
    return warnings[:10]  # Cap at 10 warnings


def _parse_json_output(raw: str) -> tuple[str, int, int, float, int]:
    """Parse Claude CLI --output-format json envelope.

    Returns (text, tokens_input, tokens_output, cost_usd, num_turns).
    Falls back to treating raw as plain text if parsing fails.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return raw, 0, 0, 0.0, 0
    try:
        d = json.loads(raw)
        usage = d.get("usage") or {}
        text = d.get("result", "")
        return (
            text,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            d.get("total_cost_usd", 0) or 0.0,
            d.get("num_turns", 0),
        )
    except (json.JSONDecodeError, TypeError, KeyError):
        return raw, 0, 0, 0.0, 0


def run(
    prompt: str,
    working_dir: str,
    timeout_minutes: int = 15,
) -> RunResult:
    claude_bin = _find_claude()
    log.info(f"Using Claude binary: {claude_bin}")

    env = os.environ.copy()
    env["PATH"] = ":".join(CLAUDE_PATHS) + ":" + env.get("PATH", "")

    cmd = [
        claude_bin,
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "-p",
        prompt,
        "--model",
        CLAUDE_MODEL,
    ]

    start = time.time()
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=working_dir,
            env=env,
            text=True,
            preexec_fn=_set_resource_limits,
        )

        timeout_secs = timeout_minutes * 60

        def _watchdog():
            nonlocal timed_out
            time.sleep(timeout_secs)
            if proc.poll() is None:
                timed_out = True
                log.warning(f"Timeout ({timeout_minutes}min) — killing Claude")
                proc.kill()

        dog = threading.Thread(target=_watchdog, daemon=True)
        dog.start()

        raw_stdout, stderr = proc.communicate()
        duration = time.time() - start

        if stderr and stderr.strip():
            log.debug("Claude stderr: %s", stderr.strip()[:500])

        text, tokens_in, tokens_out, cost, turns = _parse_json_output(raw_stdout or "")
        if tokens_in > 0:
            log.info(
                "Tokens: input=%d output=%d cost=$%.4f turns=%d", tokens_in, tokens_out, cost, turns
            )

        warnings = _scan_output(text)
        if warnings:
            log.warning("Destructive output detected (%d matches):", len(warnings))
            for w in warnings:
                log.warning("  %s", w)

        return RunResult(
            exit_code=proc.returncode or 0,
            output=text,
            timed_out=timed_out,
            duration_seconds=duration,
            destructive_warnings=warnings,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost,
            num_turns=turns,
        )

    except FileNotFoundError:
        log.error(f"Claude binary not found: {claude_bin}")
        return RunResult(
            exit_code=127, output="claude binary not found", timed_out=False, duration_seconds=0
        )
    except Exception as e:
        duration = time.time() - start
        log.error(f"Claude execution error: {e}")
        return RunResult(exit_code=1, output=str(e), timed_out=False, duration_seconds=duration)
