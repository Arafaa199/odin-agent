from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from .safety import BACKUP_RULES, safety_rules

log = logging.getLogger("prompt_builder")


CONTEXT_DIRS = [
    Path(__file__).parent.parent.parent / "config" / "context",
]


def _load_project_context(project: str, context_map: dict[str, str]) -> str:
    """Load the per-project context doc named in settings `task_runner.project_context`."""
    filename = context_map.get(project)
    if not filename:
        return ""
    for ctx_dir in CONTEXT_DIRS:
        ctx_file = ctx_dir / filename
        if ctx_file.exists():
            try:
                return ctx_file.read_text()[:4000]
            except Exception:
                continue
    return ""


def _load_servers(context_dir: Path) -> str:
    """Render config/context/servers.yaml (optional) as SSH tables for the prompt."""
    servers_file = context_dir / "servers.yaml"
    if not servers_file.exists():
        return "(no servers file configured)"

    data = yaml.safe_load(servers_file.read_text()) or {}
    key_base = data.get("key_base_path", "~/.ssh")
    lines = [f"### SSH Key Base Path\nAll keys relative to: {key_base}\n"]

    for section_key, section_label in [
        ("production", "### Production (PROD_READONLY unless +approved)"),
        ("staging", "### Staging/Testing (AUTO)"),
        ("other", "### Other"),
    ]:
        instances = data.get(section_key, [])
        if not instances:
            continue
        lines.append(section_label)
        lines.append("| Name | IP | User | Key |")
        lines.append("|------|-----|------|-----|")
        for inst in instances:
            name = inst.get("name", "")
            ip = inst.get("ip", "—")
            user = inst.get("user", "—")
            key = inst.get("key", "—")
            ssh_cmd = inst.get("ssh", "")
            if ssh_cmd:
                lines.append(f"| {name} | — | {user} | {ssh_cmd} |")
            elif key:
                lines.append(f"| {name} | {ip} | {user} | {key} |")
            else:
                lines.append(f"| {name} | {ip} | {user} | (default key) |")
        lines.append("")

    return "\n".join(lines)


def _summarize_tasks(tasks: list[dict], limit: int = 20) -> str:
    sorted_tasks = sorted(tasks, key=lambda t: t.get("urgency", 0), reverse=True)
    lines = []
    for t in sorted_tasks[:limit]:
        due = (t.get("due") or "none")[:10]
        proj = t.get("project", "?")
        desc = (t.get("description") or "")[:80]
        urg = t.get("urgency", 0)
        lines.append(f"  [{proj}] (urg:{urg:.1f}, due:{due}) {desc}")
    return "\n".join(lines) if lines else "  (no pending tasks)"


def build(
    task_json: dict,
    all_tasks: list[dict],
    tier: str,
    settings: dict,
    context_dir: Path,
    risk: str = "",
    project_profile: dict | None = None,
    memory_context: str = "",
) -> str:
    task_uuid = task_json.get("uuid", "")
    task_desc = task_json.get("description", "")
    max_minutes = settings.get("max_minutes", 15)
    state_file = str(Path(settings.get("state_file", "state.md")).expanduser())

    # Use project profile for persona and context_file, fall back to settings
    profile = project_profile or {}
    context_md = ""
    context_file_path = profile.get("context_file") or settings.get("context_file", "")
    if context_file_path:
        context_file = Path(context_file_path).expanduser()
        if context_file.exists():
            context_md = context_file.read_text()

    recent_state = ""
    state_path = Path(state_file)
    if state_path.exists():
        state_lines = state_path.read_text().splitlines()
        recent_state = "\n".join(state_lines[-200:])

    servers_section = _load_servers(context_dir)
    tasks_summary = _summarize_tasks(all_tasks)
    safety = safety_rules(tier)
    backup_section = BACKUP_RULES if risk == "BACKUP_FIRST" else ""

    tg_url = settings.get("tg_url", "http://localhost:3340")
    persona = profile.get("persona") or settings.get(
        "persona", "Odin task runner, an autonomous task executor"
    )
    task_bin = settings.get("task_bin", "task")

    prompt = f"""You are the {persona}.

Execute ONE task per run. Mark it done when complete.

## TARGET TASK
```json
{json.dumps(task_json, indent=2)}
```

## EXECUTION PROTOCOL (MANDATORY — follow this order)

### Step 1: Understand the task
Read the task description and context carefully. If you DO NOT understand what is being asked, or the task is too vague to act on, or it requires information you don't have:
- Do NOT guess or attempt a solution
- Annotate: `{task_bin} {task_uuid} annotate 'Agent: Unclear — <specific reason what is missing or ambiguous>'`
- Send notification:
  ```bash
  curl -s -X POST {tg_url}/send -H 'Content-Type: application/json' \
    -d '{{"message": "[Odin] Cannot execute: {task_desc[:80]}\\nReason: <why>\\nNeeds: <what would make this actionable>"}}'
  ```
- Exit without marking done. Do NOT retry or improvise.

### Step 2: Plan before acting
Before executing ANYTHING, output your execution plan:
- List every concrete step you will take (numbered)
- For each step, state whether it is **REVERTABLE** or **IRREVERSIBLE**
- For revertable steps, state the rollback command/procedure
- Example:
  ```
  PLAN:
  1. SSH to staging, run SELECT query → REVERTABLE (read-only)
  2. Create backup of config → REVERTABLE (delete backup file)
  3. Modify nginx config → REVERTABLE (restore from backup)
  4. Send email notification → IRREVERSIBLE
  ```

### Step 3: Consent for irreversible actions
If ANY step in your plan is IRREVERSIBLE (sending emails, external API calls, dropping data, pushing to remote, notifying third parties, modifying external systems with no undo):
- STOP before executing that step
- Request explicit consent:
  ```bash
  REPLY=$(curl -s -X POST {tg_url}/ask -H 'Content-Type: application/json' \
    -d '{{"question": "[Consent] Irreversible action: <describe what and why>. Approve?", "timeout": 300000}}')
  ```
- Parse the reply: only proceed if it contains "approve" or "yes"
- If denied or timed out: skip that step, annotate the task, continue with remaining revertable steps

### Step 4: Execute with rollback tracking
- Execute your plan step by step
- After each step, log what you did and how to revert it
- If a step fails mid-execution, attempt to revert already-completed steps before exiting
- Log all actions and rollback procedures in {state_file}

## RULES (MANDATORY)

1. Execute the target task above — ONE task only
2. Use TaskWarrior to mark done: `{task_bin} {task_uuid} done`
3. If you cannot complete: `{task_bin} {task_uuid} annotate '"Agent: Failed — <reason>"'`
4. Log evidence in {state_file} (timestamp, actions, result)
5. Max runtime: {max_minutes} minutes
6. TaskWarrior is installed locally — use `{task_bin}`

{safety}
{backup_section}

## SERVERS

{servers_section}

## TASK LIFECYCLE
```bash
# Mark done
{task_bin} {task_uuid} done

# Annotate failure
{task_bin} {task_uuid} annotate '"Agent: Failed — <reason>"'

# Annotate unclear (do NOT count as failure)
{task_bin} {task_uuid} annotate '"Agent: Unclear — <reason>"'

# Log to state
echo "### $(date '+%Y-%m-%d %H:%M:%S') — Task: {task_desc}" >> {state_file}
```

## ALL PENDING TASKS (context only — do NOT execute these)
{tasks_summary}

## RECENT STATE
```markdown
{recent_state}
```

## FORBIDDEN
- NEVER use rm or rm -rf — use: mv <path> ~/.Trash/
- NEVER git push (owner pushes)
- NEVER exceed safety tier permissions
- NEVER execute multiple tasks
- NEVER invent new tasks
- NEVER execute an irreversible action without explicit consent via {tg_url}/ask
- NEVER proceed with a task you don't fully understand — ask via bridge or annotate as unclear

NOW: Follow the Execution Protocol. Plan first, check revertability, get consent for irreversible actions, then execute.
"""

    if context_md:
        prompt += f"\n## WORK CONTEXT (CLAUDE.md)\n```markdown\n{context_md[:6000]}\n```\n"

    task_project = task_json.get("project", "")
    project_ctx = _load_project_context(task_project, settings.get("project_context", {}))
    if project_ctx:
        prompt += f"\n## PROJECT CONTEXT ({task_project})\n```markdown\n{project_ctx}\n```\n"

    if memory_context:
        prompt += f"\n## AGENT MEMORY (relevant past context)\n{memory_context}\n"

    return prompt
