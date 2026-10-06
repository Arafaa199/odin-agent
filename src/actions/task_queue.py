"""Post extracted action items to a task-queue webhook (TASK_WEBHOOK_URL)."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

import urllib.request
import urllib.error
from pathlib import Path

from ..config import vocabulary

if TYPE_CHECKING:
    from ..infra.idem_store import IdemStore
    from ..ingest.schema import ActionItem, ProcessedRecording

log = logging.getLogger("plaud-ingest")

URGENCY_TO_PRIORITY = {"high": 1, "medium": 3, "low": 5}


def _webhook_url() -> str:
    return os.environ.get("TASK_WEBHOOK_URL", "")


def _map_domain(item: ActionItem, vocab: dict | None = None) -> str | None:
    vocab = vocab or vocabulary()
    work_projects = set(vocab["projects"])
    # project OR workstream — extraction populates either, and ignoring
    # workstream misfiled genuinely-tagged work commitments.
    for field in (item.project, item.workstream):
        if field and field.lower() in work_projects:
            return "work"
    tags = {t.lower() for t in (item.tags or [])}
    for domain, domain_tags in vocab["tag_domains"].items():
        if tags & set(domain_tags):
            return domain
    # Honest NULL, not a fabricated "personal". A literal 'personal' overrode the
    # reader's own default-domain prior, so untagged work commitments (the bulk of
    # recorded meetings) leaked into the personal queue. NULL lets that prior
    # apply; a rare misrouted personal item is recoverable, a work-to-personal
    # leak is not.
    return None


def post_to_queue(
    recording: ProcessedRecording,
    items: list[ActionItem],
    idem_store: "IdemStore | None" = None,
    recording_id: str = "",
) -> list[dict]:
    results = []
    url = _webhook_url()
    if not url:
        log.info("TASK_WEBHOOK_URL not set — skipping task queue")
        return results
    vocab = vocabulary()
    for item in items:
        if item.item_type not in ("task", "decision"):
            continue

        # Skip if already posted (uses same idem key as TW)
        if idem_store and recording_id:
            idem_key = f"tq-{item.idempotency_key(recording_id)}"
            if idem_store.exists(idem_key):
                log.info(f"Task queue dedup: {item.description[:60]}")
                continue

        priority = URGENCY_TO_PRIORITY.get(item.urgency, 3)
        domain = _map_domain(item, vocab)

        stem = Path(recording.source_file).stem
        raw_content = {
            "recording": Path(recording.source_file).name,
            "transcript": f"transcripts/{stem}.json",
            "summary": recording.summary,
            "source_quote": item.source_quote,
            "source_time_start": item.source_time_start,
            "source_time_end": item.source_time_end,
            "assignee": item.assignee,
            "due_date": item.due_date,
            "due_raw": item.due_raw,
            "project": item.project,
            "item_type": item.item_type,
        }

        payload = {
            "source": "plaud",
            "source_id": f"plaud-{Path(recording.source_file).stem}-{hash(item.description) & 0xFFFFFFFF:08x}",
            "extracted_action": item.description,
            "raw_content": json.dumps(raw_content),
            "domain": domain,
            "priority": priority,
            "triage_confidence": item.confidence,
            "pre_triaged": True,
            "tier": "draft" if item.assignee == "self" else "block",
            "needs_human": True,
            "triage_reason": f"Plaud recording: {Path(recording.source_file).name} ({item.urgency} urgency, {item.confidence:.0%} confidence)",
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, context=_ssl_context(), timeout=10) as resp:
                body = json.loads(resp.read())
                results.append(
                    {
                        "success": True,
                        "task_id": body.get("task", {}).get("id"),
                        "description": item.description,
                    }
                )
                log.info(f"Queued to task_queue: {item.description[:60]}")
                if idem_store and recording_id:
                    idem_store.record(
                        idem_key=f"tq-{item.idempotency_key(recording_id)}",
                        recording_id=recording_id,
                        title=item.description[:200],
                        tw_uuid=None,
                        status="queued",
                    )
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            results.append({"success": False, "error": str(e), "description": item.description})
            log.warning(f"Failed to queue task: {item.description[:60]}: {e}")

    return results


def _ssl_context():
    """TLS context for the webhook. TASK_WEBHOOK_CA points at a private CA bundle."""
    import ssl

    ca = os.environ.get("TASK_WEBHOOK_CA", "")
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
