"""Task runner — autonomous TaskWarrior task executor.

Picks the highest-urgency eligible TaskWarrior task, classifies tier and risk,
asks for approval when needed, then executes it through the LLM router.

Usage: python -m src.task_runner [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from .actions.taskwarrior import (
    export_tasks,
    get_annotations,
    get_task_status,
    notify_desktop,
)
from . import comms
from .config import CONFIG_DIR, load_settings
from .executor.claude_runner import run as claude_run
from .llm.router import LLMRouter
from .executor.prompt_builder import build as build_prompt
from .executor.safety import classify_task_risk, detect_tier, needs_approval, requires_risk_approval
from .infra import db
from .infra.circuit_breaker import (
    clear_failures,
    is_tripped,
    record_failure,
    get_failures,
)
from .infra.lockfile import acquire as lock_acquire, release as lock_release
from .infra.memory import get_memory_client
from .infra.observability import (
    post_trace,
    start_conversation,
    add_message,
    end_conversation,
    post_event,
)

log = logging.getLogger("task_runner")

CONTEXT_DIR = CONFIG_DIR / "context"
DEFAULT_AGENT_DIR = "~/.local/state/odin/agent"

_router: LLMRouter | None = None


def _get_router() -> LLMRouter:
    """Lazy-init the LLM router with free model rotation + Claude CLI fallback."""
    global _router
    if _router is None:
        _router = LLMRouter(claude_runner_fn=claude_run)
        log.info(
            "LLM router initialized (%d free models + Claude CLI fallback)", len(_router._models)
        )
    return _router


def _load_settings() -> dict:
    return load_settings().get("task_runner", {})


def _get_project_profile(project: str, settings: dict) -> dict:
    """Look up project profile from settings, falling back to default_profile."""
    profiles = settings.get("project_profiles", {})
    default = settings.get("default_profile", {})
    profile = profiles.get(project, {})
    # Merge: default first, then project-specific overrides
    merged = dict(default)
    merged.update({k: v for k, v in profile.items() if v is not None})
    return merged


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_pending(agent_dir: Path, timeout_ms: int = 300000) -> dict | None:
    pending_file = agent_dir / ".pending-approval.json"
    if not pending_file.exists():
        return None
    try:
        data = json.loads(pending_file.read_text())
        requested = datetime.fromisoformat(data["requested_at"])
        age = (datetime.now(requested.tzinfo) - requested).total_seconds()
        # Expire after 24h hard cap
        if age > 86400:
            log.info("Pending approval expired (%.0fh old) — discarding", age / 3600)
            pending_file.unlink()
            return None
        # Auto-cleanup stale pending approvals (timeout + 60s grace)
        if data.get("status") == "pending" and age > (timeout_ms / 1000) + 60:
            log.info(
                "Pending approval timed out (%.0fs, limit %.0fs) — discarding",
                age,
                timeout_ms / 1000,
            )
            pending_file.unlink()
            return None
        # If still pending in file, check if resolved via event system
        if data.get("status") == "pending":
            resolved = _check_event_resolution(data["task_uuid"])
            if resolved:
                data["status"] = resolved
                pending_file.write_text(json.dumps(data, indent=2))
                log.info(
                    "Approval resolved via event system: %s → %s", data["task_uuid"][:8], resolved
                )
        return data
    except Exception as e:
        log.warning("Failed to load pending approval: %s", e)
        return None


def _post_approval_event(data: dict) -> str | None:
    """Post agent.approval_requested to the event intake. Returns event_id or None."""
    intake_url = os.environ.get("INTAKE_URL", "http://localhost:8100")
    intake_key = os.environ.get("INTAKE_KEY", "")
    if not intake_key:
        log.warning("INTAKE_KEY not set — skipping event write")
        return None
    try:
        resp = requests.post(
            f"{intake_url}/v1/events",
            json={
                "event_type": "agent.approval_requested",
                "priority": "P0",
                "retention_class": "permanent",
                "idempotency_key": f"agent:approval:{data['task_uuid']}",
                "payload": {
                    "task_uuid": data["task_uuid"],
                    "task_description": data["description"],
                    "task_project": data["project"],
                    "tier": data["tier"],
                    "risk": data["risk"],
                    "delivery_channels_attempted": "multi_transport",
                    "delivery_channels_succeeded": "multi_transport",
                    "delivery_status": "multi_transport",
                },
            },
            headers={"X-Intake-Key": intake_key},
            timeout=5,
        )
        if resp.status_code in (200, 201):
            eid = resp.json().get("event_id")
            log.info("Posted approval_requested event: %s", eid)
            return eid
        log.warning("Intake returned %d for approval event", resp.status_code)
    except Exception as e:
        log.warning("Failed to post approval event: %s", e)
    return None


def _check_event_resolution(task_uuid: str) -> str | None:
    """Check core.events for a matching agent.approval_resolved event."""
    if not db.is_configured():
        return None
    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload->>'action' FROM core.events
                WHERE event_type = 'agent.approval_resolved'
                  AND payload->>'task_uuid' = %s
                ORDER BY created_at DESC LIMIT 1
            """,
                (task_uuid,),
            )
            row = cur.fetchone()
        conn.close()
        if row and row[0] in ("approved", "declined"):
            return row[0]
    except Exception as e:
        log.warning("Event resolution check failed: %s", e)
    return None


def _save_pending(agent_dir: Path, data: dict) -> None:
    pending_file = agent_dir / ".pending-approval.json"
    pending_file.write_text(json.dumps(data, indent=2))
    _post_approval_event(data)


def _clear_pending(agent_dir: Path) -> None:
    pending_file = agent_dir / ".pending-approval.json"
    if pending_file.exists():
        pending_file.unlink()


def _has_unclear_annotation(task: dict) -> bool:
    """Check if task has an unresolved 'Agent: Unclear' annotation."""
    for ann in task.get("annotations", []):
        desc = ann.get("description", "")
        if "Agent: Unclear" in desc:
            return True
    return False


def _pick_task(tasks: list[dict], failure_path: str, max_failures: int) -> dict | None:
    eligible = []
    for t in tasks:
        tags = t.get("tags", [])
        if "odin" not in tags and "demon" not in tags and "operator" not in tags:
            continue
        if "blocked" in tags or "deferred" in tags:
            continue
        uuid = t.get("uuid", "")
        if is_tripped(uuid, failure_path, max_failures):
            continue
        if _has_unclear_annotation(t):
            continue
        eligible.append(t)

    if not eligible:
        return None

    eligible.sort(key=lambda t: t.get("urgency", 0), reverse=True)
    return eligible[0]


def _ssh_connectivity_check(profile: dict, task_desc: str) -> bool:
    """Probe the profile's server before running a task that targets it.

    Profile key `ssh_connectivity_test`: {host, user, key, match}. `match` is a
    regex over the task description; without it every task in the profile probes.
    """
    ssh_cfg = profile.get("ssh_connectivity_test") or {}
    host = ssh_cfg.get("host")
    if not host:
        return True

    pattern = ssh_cfg.get("match")
    if pattern and not re.search(pattern, task_desc, re.IGNORECASE):
        return True

    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if ssh_cfg.get("key"):
        cmd += ["-i", str(Path(ssh_cfg["key"]).expanduser())]
    user = ssh_cfg.get("user")
    target = f"{user}@{host}" if user else host
    try:
        result = subprocess.run([*cmd, target, "echo ok"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Odin task runner")
    parser.add_argument("--force", action="store_true", help="Skip lockfile check")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    settings = _load_settings()
    if not settings:
        log.error("No task_runner section in settings")
        sys.exit(1)

    agent_dir = Path(settings.get("agent_dir", DEFAULT_AGENT_DIR)).expanduser()
    enabled_file = agent_dir / ".enabled"
    lock_path = agent_dir / ".lck"
    failure_path = agent_dir / ".task-failures"
    state_file = Path(settings.get("state_file", str(agent_dir / "state.md"))).expanduser()
    log_dir = agent_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    session_log = log_dir / f"session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    file_handler = logging.FileHandler(session_log)
    file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s: %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(file_handler)

    if not enabled_file.exists():
        log.info("Task runner disabled. Enable: touch %s", enabled_file)
        sys.exit(0)

    if not lock_acquire(lock_path, force=args.force):
        log.info("Already running (lockfile fresh). Use --force to override.")
        sys.exit(0)

    # Init transport registry from top-level config
    comms.init_transports(load_settings())

    max_minutes = settings.get("max_minutes", 15)
    max_failures = settings.get("max_task_failures", 3)
    all_projects = settings.get("all_projects", ["demo"])

    log.info("=== Task Runner Session Start [holistic: %d projects] ===", len(all_projects))
    session_start = time.time()

    try:
        _run(
            settings=settings,
            agent_dir=agent_dir,
            failure_path=str(failure_path),
            state_file=state_file,
            max_minutes=max_minutes,
            max_failures=max_failures,
            all_projects=all_projects,
        )
    except Exception as e:
        log.error(f"Unhandled error: {e}", exc_info=True)
    finally:
        duration = int(time.time() - session_start)
        log.info(f"Duration: {duration}s")
        log.info("=== Session Complete ===")
        lock_release(lock_path)
        _cleanup_logs(log_dir)


def _record_failure_and_alert(
    task_uuid: str,
    task_desc: str,
    task_project: str,
    failure_path: str,
    max_failures: int,
) -> int:
    """Record failure and send alert if circuit just tripped."""
    count = record_failure(task_uuid, failure_path)
    if count >= max_failures:
        log.warning(f"CIRCUIT TRIPPED for {task_uuid[:8]} after {count} failures")
        comms.send(
            f"🔴 CIRCUIT TRIPPED: [{task_project}] {task_desc}\n"
            f"{count}/{max_failures} failures. Auto-retry in 48h.\n"
            f"Manual reset: edit .task-failures in the agent dir",
        )
    return count


def _execute_task(
    task: dict,
    tasks: list[dict],
    tier: str,
    risk: str,
    settings: dict,
    profile: dict,
    failure_path: str,
    state_file: Path,
    max_minutes: int,
    max_failures: int,
) -> None:
    task_uuid = task.get("uuid", "")
    task_desc = task.get("description", "")
    task_project = task.get("project", "")
    fail_count = get_failures(task_uuid, failure_path)
    work_dir = str(Path(profile.get("work_dir", ".")).expanduser())
    task_summary = f"[{task_project}] {task_desc}"
    model = os.environ.get("CLAUDE_MODEL", "sonnet")
    time.time()

    # ── Emit task_picked event ────────────────────────────────────────
    post_event(
        "agent.task_picked",
        {
            "agent": "task_runner",
            "task_uuid": task_uuid,
            "task_description": task_desc,
            "task_project": task_project,
            "tier": tier,
            "risk": risk,
            "fail_count": fail_count,
        },
    )

    # ── Start conversation ────────────────────────────────────────────
    session_id = f"task-runner-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    conv_id = start_conversation(
        "task_runner",
        session_id,
        metadata={
            "task_uuid": task_uuid,
            "task_project": task_project,
            "tier": tier,
            "risk": risk,
        },
    )

    # ── Memory: recall relevant context ─────────────────────────────
    memory_context = ""
    try:
        mem = get_memory_client()
        if mem:
            memory_context = mem.recall(f"{task_project} {task_desc}", limit=3)
    except Exception as e:
        log.debug("Memory recall failed: %s", e)

    prompt = build_prompt(
        task_json=task,
        all_tasks=tasks,
        tier=tier,
        settings=settings,
        context_dir=CONTEXT_DIR,
        risk=risk,
        project_profile=profile,
        memory_context=memory_context,
    )

    add_message(conv_id, "user", prompt)

    task_tags = frozenset(task.get("tags", []))
    router = _get_router()
    log.info("Routing task through LLM router (tags=%s, tier=%s)...", task_tags, tier)
    result = router.run(
        prompt,
        tags=task_tags,
        tier=tier,
        working_dir=work_dir,
        timeout_minutes=max_minutes,
    )
    model = result.model  # override model var with actual model used

    if result.output:
        log.info(
            "LLM output (%d chars, backend=%s, model=%s, attempts=%d)",
            len(result.output),
            result.backend,
            result.model,
            result.attempts,
        )
        log.info(
            "Tokens: input=%d output=%d cost=$%.4f",
            result.tokens_input,
            result.tokens_output,
            result.cost_usd,
        )

    add_message(conv_id, "assistant", result.output or "", tokens=result.tokens_output or None)

    duration = int(result.duration_seconds)
    duration_ms = int(result.duration_seconds * 1000)
    output_summary = (result.output or "")[:500]

    # Alert on destructive output patterns
    if result.destructive_warnings:
        warning_text = "\n".join(result.destructive_warnings[:5])
        log.warning("Destructive output flagged:\n%s", warning_text)
        comms.send(
            f"⚠️ DESTRUCTIVE OUTPUT [{risk}]: [{task_project}] {task_desc}\n{warning_text}",
        )

    if result.timed_out:
        log.warning(f"TIMEOUT: Session exceeded {max_minutes}min budget")
        fail_count = _record_failure_and_alert(
            task_uuid, task_desc, task_project, failure_path, max_failures
        )
        comms.send(
            f"TIMEOUT [{risk}]: [{task_project}] {task_desc} — exceeded {max_minutes}min\n"
            f"Output: {output_summary}",
        )
        _append_state(
            state_file,
            task_desc,
            task_uuid,
            task_project,
            tier,
            f"TIMEOUT (attempt {fail_count}/{max_failures})",
        )
        end_conversation(conv_id, f"TIMEOUT: {task_summary}", 2)
        post_trace(
            "task_runner",
            "timeout",
            task_summary,
            result.tokens_input,
            result.tokens_output,
            result.cost_usd,
            model,
            duration_ms,
            ["timeout"],
            result.num_turns,
        )
        post_event(
            "agent.task_result",
            {
                "agent": "task_runner",
                "task_uuid": task_uuid,
                "outcome": "timeout",
                "duration_s": duration,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "cost_usd": result.cost_usd,
            },
        )
        return

    if result.exit_code not in (0, None):
        log.warning(f"Claude exited with code {result.exit_code}")
        _record_failure_and_alert(task_uuid, task_desc, task_project, failure_path, max_failures)

    status = get_task_status(task_uuid)
    annotations = get_annotations(task_uuid, limit=5)

    # Check if Claude flagged the task as unclear (don't burn circuit breaker)
    is_unclear = any("Agent: Unclear" in a for a in annotations)

    if status == "completed":
        outcome = "success"
        log.info(f"Task COMPLETED: {task_desc}")
        clear_failures(task_uuid, failure_path)
        comms.send(
            f"DONE [{risk}]: [{task_project}] {task_desc} ({duration}s)\nOutput: {output_summary}",
        )
        _append_state(state_file, task_desc, task_uuid, task_project, tier, "DONE")
    elif is_unclear:
        outcome = "unclear"
        log.info(f"Task UNCLEAR (not a failure): {task_desc}")
        unclear_reasons = [a for a in annotations if "Agent: Unclear" in a]
        reason_text = unclear_reasons[-1] if unclear_reasons else "no reason given"
        comms.send(
            f"UNCLEAR: [{task_project}] {task_desc}\n"
            f"{reason_text}\n"
            f"Add context or rephrase, then 'run' to retry.",
        )
        _append_state(
            state_file,
            task_desc,
            task_uuid,
            task_project,
            tier,
            "UNCLEAR — needs human input",
            extra=f"- **Reason**: {reason_text}",
        )
    else:
        outcome = "failed"
        log.warning(f"Task NOT completed (status: {status})")
        fail_count = _record_failure_and_alert(
            task_uuid, task_desc, task_project, failure_path, max_failures
        )
        ann_text = (
            "\n".join(f"  - {a}" for a in annotations) if annotations else "  (no annotations)"
        )

        comms.send(
            f"FAILED [{risk}]: [{task_project}] {task_desc} "
            f"(attempt {fail_count}/{max_failures}, {duration}s)\n"
            f"Output: {output_summary}",
        )
        _append_state(
            state_file,
            task_desc,
            task_uuid,
            task_project,
            tier,
            f"FAILED (attempt {fail_count}/{max_failures})",
            extra=f"- **Annotations**:\n{ann_text}",
        )

    # ── Memory: save task outcome ────────────────────────────────────
    try:
        mem = get_memory_client()
        if mem:
            tags = [outcome, task_project]
            mem.save(
                f"Task {outcome}: [{task_project}] {task_desc} ({duration}s)",
                category="state",
                tags=tags,
                confidence=0.85 if outcome == "success" else 0.7,
            )
    except Exception as e:
        log.debug("Memory save failed: %s", e)

    # ── Record observability ──────────────────────────────────────────
    errors = []
    if outcome == "failed":
        errors = [a for a in annotations][:5] if annotations else ["task not completed"]
    elif outcome == "unclear":
        errors = ["task unclear — needs human input"]

    end_conversation(conv_id, f"{outcome.upper()}: {task_summary}", 2)
    post_trace(
        "task_runner",
        outcome,
        task_summary,
        result.tokens_input,
        result.tokens_output,
        result.cost_usd,
        model,
        duration_ms,
        errors if errors else None,
        result.num_turns,
    )
    post_event(
        "agent.task_result",
        {
            "agent": "task_runner",
            "task_uuid": task_uuid,
            "outcome": outcome,
            "duration_s": duration,
            "task_project": task_project,
            "tokens_input": result.tokens_input,
            "tokens_output": result.tokens_output,
            "cost_usd": result.cost_usd,
        },
    )

    if profile.get("watch_repo"):
        _warn_uncommitted(profile["watch_repo"])


def _run(
    settings: dict,
    agent_dir: Path,
    failure_path: str,
    state_file: Path,
    max_minutes: int,
    max_failures: int,
    all_projects: list[str],
) -> None:
    # ── Check for pre-approved pending task (triggered by bridge) ──
    tg_timeout = settings.get("tg_approval_timeout_ms", 300000)
    pending = _load_pending(agent_dir, timeout_ms=tg_timeout)
    if pending:
        if pending.get("status") == "approved":
            # UUID verification: confirm approved task still exists and matches
            approved_uuid = pending.get("task_uuid", "")
            task = pending["task_json"]
            task_uuid_in_json = task.get("uuid", "")
            if approved_uuid and approved_uuid != task_uuid_in_json:
                log.error(
                    "UUID mismatch in approval: expected %s, got %s — discarding",
                    approved_uuid[:8],
                    task_uuid_in_json[:8],
                )
                _clear_pending(agent_dir)
                comms.send(
                    "[Odin] Approval rejected: task UUID mismatch (possible race condition)"
                )
                return
            # Verify task is still pending in TW
            current_status = get_task_status(approved_uuid)
            if current_status != "pending":
                log.info(
                    "Approved task no longer pending (status: %s) — discarding", current_status
                )
                _clear_pending(agent_dir)
                return
            log.info("Executing pre-approved task: %s", pending.get("description"))
            _clear_pending(agent_dir)
            tier = pending.get("tier", "AUTO")
            risk = pending.get("risk", "CONFIRM_APPROACH")
            task_project = task.get("project", "")
            profile = _get_project_profile(task_project, settings)
            comms.send(
                f"[Odin] Executing approved task: [{task_project}] "
                f"{task.get('description', '')}",
            )
            _execute_task(
                task=task,
                tasks=pending.get("all_tasks", []),
                tier=tier,
                risk=risk,
                settings=settings,
                profile=profile,
                failure_path=failure_path,
                state_file=state_file,
                max_minutes=max_minutes,
                max_failures=max_failures,
            )
            return
        elif pending.get("status") == "declined":
            log.info("Pending task was declined: %s", pending.get("description"))
            _clear_pending(agent_dir)
            return
        elif pending.get("status") == "pending":
            log.info("Pending approval still waiting — skipping new task pick")
            return

    tasks = export_tasks(all_projects)
    if not tasks:
        log.info("No eligible pending tasks.")
        post_trace("task_runner", "idle", "no pending tasks")
        return

    task = _pick_task(tasks, failure_path, max_failures)
    if not task:
        log.info("No eligible pending tasks (all blocked/circuit-broken).")
        post_trace("task_runner", "idle", "all tasks blocked/circuit-broken")
        return

    task_uuid = task.get("uuid", "")
    task_desc = task.get("description", "")
    task_project = task.get("project", "")
    task_tags = task.get("tags", [])
    fail_count = get_failures(task_uuid, failure_path)

    # Resolve per-project profile (work_dir, persona, context_file, domain, ssh)
    profile = _get_project_profile(task_project, settings)
    domain = profile.get("domain", "work")
    work_dir = str(Path(profile.get("work_dir", ".")).expanduser())

    log.info(f"Selected: [{task_project}] {task_desc} (uuid: {task_uuid[:8]}..., domain: {domain})")

    # Safety tier (domain-aware)
    tier = detect_tier(task_desc, task_tags, domain=domain)
    log.info(f"Safety tier: {tier}")

    # Production/critical write without approval → skip
    if needs_approval(task_desc, tier):
        log.info("SKIP: Production/critical write detected without +approved tag")
        _write_approval_needed(task_uuid, task_project, task_desc, work_dir)
        comms.send(
            f"APPROVAL NEEDED: [{task_project}] {task_desc} — requires +approved tag. "
            f"Run: task {task_uuid} modify +approved",
        )
        return

    # SSH connectivity check for server tasks (from project profile)
    if not _ssh_connectivity_check(profile, task_desc):
        log.info("SKIP: SSH connectivity check failed")
        _record_failure_and_alert(task_uuid, task_desc, task_project, failure_path, max_failures)
        comms.send(f"SSH unreachable — skipping [{task_project}] {task_desc}")
        return

    # Risk-based approval (combined with tier)
    risk = classify_task_risk(task_desc, task_tags)
    log.info(f"Risk level: {risk}")

    if requires_risk_approval(tier, risk):
        # Save pending approval and notify all channels — don't block
        prefix = "\u26a0\ufe0f DESTRUCTIVE" if risk == "DESTRUCTIVE" else "Approach check"
        _save_pending(
            agent_dir,
            {
                "task_uuid": task_uuid,
                "task_json": task,
                "all_tasks": tasks,
                "risk": risk,
                "tier": tier,
                "description": task_desc,
                "project": task_project,
                "requested_at": datetime.now().astimezone().isoformat(),
                "status": "pending",
            },
        )
        comms.send_all(
            f"[Odin] {prefix}: [{task_project}] {task_desc}\n"
            f"Tier: {tier} | Risk: {risk} | Failures: {fail_count}/{max_failures}\n"
            f"Reply 'approve' to execute or 'skip' to decline.",
        )
        log.info("Saved pending approval and notified all channels — exiting")
        return
    elif risk == "BACKUP_FIRST":
        log.info("Auto-approved with backup mandate (risk: BACKUP_FIRST)")
    else:
        log.info("Auto-approved — non-destructive (risk: SAFE)")

    _execute_task(
        task=task,
        tasks=tasks,
        tier=tier,
        risk=risk,
        settings=settings,
        profile=profile,
        failure_path=failure_path,
        state_file=state_file,
        max_minutes=max_minutes,
        max_failures=max_failures,
    )


def _write_approval_needed(uuid: str, project: str, desc: str, work_dir: str) -> None:
    content = (
        f"# Odin Task Runner — Approval Required\n\n"
        f"Generated: {_timestamp()}\n\n"
        f"Task requires production write access but lacks +approved tag:\n\n"
        f"- **UUID**: {uuid}\n"
        f"- **Project**: {project}\n"
        f"- **Description**: {desc}\n\n"
        f"To approve: task {uuid} modify +approved\n"
    )
    notify_desktop("ODIN_APPROVAL_NEEDED.txt", content)


def _append_state(
    state_file: Path,
    desc: str,
    uuid: str,
    project: str,
    tier: str,
    result: str,
    extra: str = "",
) -> None:
    entry = (
        f"\n### {_timestamp()} — Task: {desc}\n"
        f"- **UUID**: {uuid[:8]}\n"
        f"- **Project**: {project}\n"
        f"- **Tier**: {tier}\n"
        f"- **Result**: {result}\n"
    )
    if extra:
        entry += f"{extra}\n"

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "a") as f:
            f.write(entry)
    except Exception as e:
        log.error(f"Failed to append to state: {e}")


def _warn_uncommitted(repo_dir: str) -> None:
    """Log a warning if the task left uncommitted changes in the watched repo."""
    repo = Path(repo_dir).expanduser()
    if not (repo / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=10,
        )
        count = len([l for l in result.stdout.splitlines() if l.strip()])
        if count > 0:
            log.warning(f"{count} uncommitted changes in {repo}")
    except Exception as e:
        log.debug(f"Uncommitted-changes check failed for {repo}: {e}")


def _cleanup_logs(log_dir: Path, keep: int = 50) -> None:
    logs = sorted(log_dir.glob("session-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
