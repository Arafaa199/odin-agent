from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from ..config import load_settings, vocabulary
from ..ingest.schema import ActionItem, ProcessedRecording
from ..infra.ssh import write_file
from ..infra.idem_store import IdemStore

log = logging.getLogger("taskwarrior")

URGENCY_TO_PRIORITY = {"high": "H", "medium": "M", "low": "L"}

# Controlled tag vocabulary (settings `vocabulary`), the same list the extraction
# prompt offers. Only these LLM-supplied tags survive onto a task; freeform
# entity tags (person names, company names, acronyms) are dropped.
ALLOWED_TAGS = frozenset(vocabulary()["allowed_tags"])


def _run_local(cmd_parts: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a TaskWarrior command locally (TW installed on this machine)."""
    return subprocess.run(
        cmd_parts,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _load_tw_settings() -> dict:
    return load_settings().get("taskwarrior", {})


def notify_desktop(filename: str, body: str) -> bool:
    """Drop a note file where the owner will see it.

    NOTIFY_DIR (default ~/odin-notifications) on this machine, or on
    NOTIFY_SSH_HOST when set (for example a laptop's Desktop).
    """
    safe_name = filename.replace("/", "_").replace("\\", "_")
    notify_dir = os.environ.get("NOTIFY_DIR", "~/odin-notifications")
    ssh_host = os.environ.get("NOTIFY_SSH_HOST", "")
    if ssh_host:
        ok = write_file(ssh_host, f"{notify_dir.rstrip('/')}/{safe_name}", body)
    else:
        ok = _write_local(Path(notify_dir).expanduser() / safe_name, body)
    if ok:
        log.info(f"Desktop notification: {safe_name}")
    else:
        log.error(f"Failed to write desktop notification: {safe_name}")
    return ok


def _write_local(path: Path, body: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return True
    except OSError as e:
        log.error(f"Failed to write {path}: {e}")
        return False


def create_task(
    item: ActionItem,
    source_file: str,
    tw_cfg: dict | None = None,
    idem_store: IdemStore | None = None,
    recording_id: str = "",
) -> dict:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()

    executable = tw_cfg.get("executable", "task")

    # Idempotency check (before assignee branching — applies to both self and delegated)
    idem_key = item.idempotency_key(recording_id) if recording_id else None
    if idem_key and idem_store and idem_store.exists(idem_key):
        log.info(f"Dedup: already created — {item.description[:60]} (key={idem_key})")
        return {"uuid": None, "success": False, "deduped": True, "error": "duplicate"}

    # Build description — prefix with @assignee for delegated tasks
    is_delegated = item.assignee not in ("self", "unknown")
    description = item.description
    if is_delegated:
        description = f"@{item.assignee}: {description}"

    cmd = [executable, "add", description]

    priority = URGENCY_TO_PRIORITY.get(item.urgency)
    if priority:
        cmd.append(f"priority:{priority}")

    project = item.project or tw_cfg.get("default_project")
    if project:
        cmd.append(f"project:{project}")

    if item.workstream:
        cmd.append(f"workstream:{item.workstream}")

    if item.due_date:
        cmd.append(f"due:{item.due_date}")

    if item.requires_confirmation:
        cmd.append("+confirm")

    if is_delegated:
        cmd.append("+delegated")

    always_tags = set(tw_cfg.get("always_tag", ["plaud"])) | {"inbox"}
    allowed_llm_tags = {t.lower() for t in item.tags} & ALLOWED_TAGS
    all_tags = always_tags | allowed_llm_tags
    for tag in sorted(all_tags):
        cmd.append(f"+{tag}")

    try:
        cmd.insert(1, "rc.verbose=new-uuid")
        result = _run_local(cmd)
        if result.returncode != 0:
            log.error(f"task add failed: {result.stderr.strip()}")
            return {"uuid": None, "success": False, "error": result.stderr.strip()}

        # Parse UUID from "Created task <uuid>." output
        uuid = None
        for line in result.stdout.strip().split("\n"):
            if "Created task" in line:
                parts = line.split()
                for p in parts:
                    if len(p) >= 8 and "-" in p:
                        uuid = p.rstrip(".")
                        break
        if not uuid:
            # Fallback: try +LATEST (less reliable but better than nothing)
            uuid_result = _run_local(
                [executable, "rc.verbose=nothing", "+LATEST", "uuids"], timeout=10
            )
            uuid = uuid_result.stdout.strip() if uuid_result.returncode == 0 else None

        if uuid:
            # Source annotation — recording + transcript for traceback
            stem = Path(source_file).stem
            ann = f"Source: {Path(source_file).name} | Transcript: transcripts/{stem}.json"
            if item.source_time_start:
                ann += f" [{item.source_time_start}"
                if item.source_time_end:
                    ann += f"-{item.source_time_end}"
                ann += "]"
            if item.source_quote:
                ann += f' | "{item.source_quote[:120]}"'
            _run_local([executable, uuid, "annotate", ann], timeout=10)

            # Due context — what was actually said, if the resolved date is uncertain
            if item.due_raw and item.due_confidence < 0.9:
                _run_local(
                    [
                        executable,
                        uuid,
                        "annotate",
                        f'Said: "{item.due_raw}" (confidence: {item.due_confidence:.0%})',
                    ],
                    timeout=10,
                )

            if item.confirmation_question:
                _run_local(
                    [executable, uuid, "annotate", f"CONFIRM: {item.confirmation_question}"],
                    timeout=10,
                )

        # Record in idempotency store
        if idem_key and idem_store:
            idem_store.record(
                idem_key=idem_key,
                recording_id=recording_id,
                title=item.description[:200],
                tw_uuid=uuid,
                status="created",
            )

        log.info(f"Created task: {item.description[:60]} (uuid={uuid})")
        return {"uuid": uuid, "success": True, "error": None}

    except subprocess.TimeoutExpired:
        log.error("TaskWarrior command timed out")
        return {"uuid": None, "success": False, "error": "tw_timeout"}
    except Exception as e:
        log.error(f"task add failed: {e}")
        return {"uuid": None, "success": False, "error": str(e)}


def create_tasks(
    recording: ProcessedRecording,
    threshold: float | None = None,
    tw_cfg: dict | None = None,
    idem_store: IdemStore | None = None,
    summaries_dir: Path | None = None,
) -> list[dict]:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()

    if not tw_cfg.get("enabled", True):
        log.info("TaskWarrior integration disabled in config")
        return []

    if threshold is None:
        threshold = tw_cfg.get("auto_create_threshold", 0.7)

    recording_id = hashlib.sha256(Path(recording.source_file).name.encode()).hexdigest()[:12]

    self_items: list[ActionItem] = []
    non_self_items: list[ActionItem] = []
    low_confidence: list[ActionItem] = []
    filtered_out: list[ActionItem] = []

    for item in recording.action_items:
        # Only actionable item types become TW tasks — skip info/question (parity with task_queue.py)
        if item.item_type not in ("task", "decision"):
            filtered_out.append(item)
            log.info(f"Filtered ({item.item_type}): {item.description[:60]}")
            continue
        if item.confidence < threshold:
            low_confidence.append(item)
            log.info(f"Low-confidence ({item.confidence:.2f}): {item.description[:60]}")
        elif item.assignee != "self":
            non_self_items.append(item)
            log.info(f"Non-self @{item.assignee}: {item.description[:60]}")
        else:
            self_items.append(item)

    self_results: list[dict] = [
        create_task(item, recording.source_file, tw_cfg, idem_store, recording_id)
        for item in self_items
    ]
    non_self_results: list[dict] = [
        create_task(item, recording.source_file, tw_cfg, idem_store, recording_id)
        for item in non_self_items
    ]
    results = self_results + non_self_results

    created_self = sum(1 for r in self_results if r["success"])
    created_delegated = sum(1 for r in non_self_results if r["success"])
    created = created_self + created_delegated
    non_self_count = len(non_self_items)
    skipped = len(low_confidence)
    filtered_count = len(filtered_out)

    log.info(
        f"TaskWarrior: {created} created ({created_self} self, {created_delegated} delegated), "
        f"{skipped} low-confidence, {filtered_count} filtered (info/question) "
        f"from {Path(recording.source_file).name}"
    )

    # Write summary to local summaries directory
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    source_name = Path(recording.source_file).stem[:30]

    transcript_name = f"transcripts/{Path(recording.source_file).stem}.json"
    lines = [
        f"Plaud Recording Processed — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Source: {Path(recording.source_file).name}",
        f"Transcript: {transcript_name}",
        f"Duration: {recording.duration_seconds:.0f}s",
        f"Participants: {', '.join(recording.participants)}",
        "",
        f"Summary: {recording.summary}",
        "",
        f"Tasks created: {created} ({created_self} self, {created_delegated} delegated)",
    ]
    for item, result in zip(self_items, self_results):
        if result.get("deduped"):
            status = "DEDUP"
        elif result["success"]:
            status = "OK"
        else:
            status = "FAILED"
        lines.append(f"  [{status}] {item.description}")

    if non_self_items:
        lines.append(f"\nDelegated to others ({non_self_count}):")
        for item, result in zip(non_self_items, non_self_results):
            if result.get("deduped"):
                status = "DEDUP"
            elif result["success"]:
                status = "OK"
            else:
                status = "FAILED"
            lines.append(f"  [{status}] @{item.assignee}: {item.description}")

    if low_confidence:
        lines.append(f"\nLow confidence — skipped ({skipped}):")
        for item in low_confidence:
            lines.append(f"  ({item.confidence:.0%}) {item.description}")

    if filtered_out:
        lines.append(f"\nNon-actionable — filtered ({filtered_count}):")
        for item in filtered_out:
            lines.append(f"  [{item.item_type}] {item.description}")

    if recording.key_decisions:
        lines.append("\nKey decisions:")
        for d in recording.key_decisions:
            lines.append(f"  - {d}")

    if summaries_dir:
        try:
            summaries_dir.mkdir(parents=True, exist_ok=True)
            summary_file = summaries_dir / f"PLAUD_{ts}_{source_name}.txt"
            summary_file.write_text("\n".join(lines))
            log.info(f"Summary written: {summary_file}")
        except Exception as e:
            log.warning(f"Failed to write summary file: {e}")

    # Notification — if any tasks were created
    if created:
        try:
            from .. import comms

            tg_lines = ["*Plaud Recording Processed*"]
            tg_lines.append(f"_{recording.summary[:150]}_")
            tg_lines.append(
                f"\n*Tasks created ({created_self} self, {created_delegated} delegated):*"
            )
            for item in self_items:
                confirm = " ⚠️ needs confirm" if item.requires_confirmation else ""
                due = f" (due {item.due_date})" if item.due_date else ""
                tg_lines.append(f"• {item.description}{due}{confirm}")
            for item in non_self_items:
                due = f" (due {item.due_date})" if item.due_date else ""
                tg_lines.append(f"• @{item.assignee}: {item.description}{due}")
            comms.send("\n".join(tg_lines))
        except Exception as e:
            log.warning(f"Notification failed (non-fatal): {e}")

    return results


def export_tasks(projects: list[str], tw_cfg: dict | None = None) -> list[dict]:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()
    executable = tw_cfg.get("executable", "task")

    project_filter = " or ".join(f"project:{p}" for p in projects)
    cmd = [
        executable,
        "rc.verbose=nothing",
        "rc.json.array=on",
        f"( {project_filter} )",
        "status:pending",
        "export",
    ]

    try:
        result = _run_local(cmd, timeout=15)
        if result.returncode != 0:
            log.error(f"task export failed: {result.stderr.strip()}")
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse task export JSON: {e}")
        return []
    except subprocess.TimeoutExpired:
        log.error("TaskWarrior command timed out during export")
        return []
    except Exception as e:
        log.error(f"task export failed: {e}")
        return []


def get_task_status(uuid: str, tw_cfg: dict | None = None) -> str:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()
    executable = tw_cfg.get("executable", "task")

    try:
        result = _run_local(
            [executable, "rc.verbose=nothing", uuid, "export"],
            timeout=10,
        )
        if result.returncode != 0:
            return "unknown"
        data = json.loads(result.stdout)
        if isinstance(data, list) and data:
            return data[0].get("status", "unknown")
        return "unknown"
    except Exception:
        return "unknown"


def mark_done(uuid: str, tw_cfg: dict | None = None) -> bool:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()
    executable = tw_cfg.get("executable", "task")

    try:
        result = _run_local(
            [executable, "rc.confirmation=off", uuid, "done"],
            timeout=15,
        )
        ok = result.returncode == 0
        if ok:
            log.info(f"Marked done: {uuid[:8]}")
        else:
            log.error(f"mark_done failed: {result.stderr.strip()}")
        return ok
    except Exception as e:
        log.error(f"mark_done failed: {e}")
        return False


def annotate_task(uuid: str, annotation: str, tw_cfg: dict | None = None) -> bool:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()
    executable = tw_cfg.get("executable", "task")

    try:
        result = _run_local(
            [executable, uuid, "annotate", annotation],
            timeout=10,
        )
        ok = result.returncode == 0
        if ok:
            log.info(f"Annotated {uuid[:8]}: {annotation[:60]}")
        else:
            log.error(f"annotate failed: {result.stderr.strip()}")
        return ok
    except Exception as e:
        log.error(f"annotate failed: {e}")
        return False


def get_annotations(uuid: str, limit: int = 3, tw_cfg: dict | None = None) -> list[str]:
    if tw_cfg is None:
        tw_cfg = _load_tw_settings()
    executable = tw_cfg.get("executable", "task")

    try:
        result = _run_local(
            [executable, "rc.verbose=nothing", uuid, "export"],
            timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        if isinstance(data, list) and data:
            anns = data[0].get("annotations", [])
            return [a.get("description", "") for a in anns[-limit:]]
        return []
    except Exception:
        return []
