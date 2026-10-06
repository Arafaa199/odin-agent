"""Observability helpers — trace, conversation, and event recording.

Writes to:
  - ops.agent_traces (via psycopg2)
  - memory.conversations + memory.messages (via psycopg2)
  - core.events (via the event intake HTTP API)
"""

from __future__ import annotations

import logging
import os
import uuid

import requests

from . import db

log = logging.getLogger("observability")

_INTAKE_URL = os.environ.get("INTAKE_URL", "http://localhost:8100")
_INTAKE_KEY = os.environ.get("INTAKE_KEY", "")


def _get_conn():
    return db.connect()


# ── Trace helpers ─────────────────────────────────────────────────────────


def post_trace(
    agent: str,
    outcome: str,
    task_summary: str,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cost_usd: float = 0.0,
    model: str | None = None,
    duration_ms: int = 0,
    errors: list[str] | None = None,
    num_turns: int = 0,
) -> None:
    if not db.is_configured():
        log.warning("ODIN_DB_PASSWORD not set — skipping trace write")
        return
    try:
        import json as _json

        errors_json = _json.dumps(errors or [])
        conn = _get_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops.agent_traces (
                        agent, outcome, task_summary, tool_calls,
                        tokens_input, tokens_output, estimated_cost_usd,
                        model, duration_ms, errors, ended_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                """,
                    (
                        agent,
                        outcome,
                        task_summary,
                        num_turns,
                        tokens_input,
                        tokens_output,
                        cost_usd,
                        model,
                        duration_ms,
                        errors_json,
                    ),
                )
            log.info("Trace recorded: %s/%s (cost=$%.4f)", agent, outcome, cost_usd)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to post trace: %s", e)


# ── Conversation helpers ──────────────────────────────────────────────────


def start_conversation(agent: str, session_id: str, metadata: dict | None = None) -> str | None:
    if not db.is_configured():
        log.warning("ODIN_DB_PASSWORD not set — skipping conversation start")
        return None
    try:
        import json as _json

        meta_json = _json.dumps(metadata or {})
        conn = _get_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory.conversations (agent, session_id, started_at, metadata)
                    VALUES (%s, %s, now(), %s::jsonb)
                    RETURNING id
                """,
                    (agent, session_id, meta_json),
                )
                row = cur.fetchone()
            conv_id = str(row[0]) if row else None
            log.info("Conversation started: %s", conv_id)
            return conv_id
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to start conversation: %s", e)
        return None


def add_message(conv_id: str | None, role: str, content: str, tokens: int | None = None) -> None:
    if not conv_id or not db.is_configured():
        return
    try:
        content = content[:50000]
        conn = _get_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memory.messages (conversation_id, role, content, tokens_used, created_at)
                    VALUES (%s::uuid, %s, %s, %s, now())
                """,
                    (conv_id, role, content, tokens),
                )
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to add message: %s", e)


def end_conversation(conv_id: str | None, summary: str, message_count: int = 0) -> None:
    if not conv_id or not db.is_configured():
        return
    try:
        conn = _get_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory.conversations
                    SET ended_at = now(), summary = %s, message_count = %s
                    WHERE id = %s::uuid
                """,
                    (summary, message_count, conv_id),
                )
            log.info("Conversation ended: %s", conv_id)
        finally:
            conn.close()
    except Exception as e:
        log.warning("Failed to end conversation: %s", e)


# ── Intake event helpers ──────────────────────────────────────────────────


def post_event(event_type: str, payload: dict, priority: str = "P2") -> str | None:
    if not _INTAKE_KEY:
        log.warning("INTAKE_KEY not set — skipping event")
        return None
    idem_key = f"odin:{event_type}:{uuid.uuid4().hex[:12]}"
    try:
        resp = requests.post(
            f"{_INTAKE_URL}/v1/events",
            json={
                "event_type": event_type,
                "priority": priority,
                "retention_class": "short",
                "idempotency_key": idem_key,
                "payload": payload,
            },
            headers={"X-Intake-Key": _INTAKE_KEY},
            timeout=5,
        )
        if resp.status_code in (200, 201):
            eid = resp.json().get("event_id")
            log.info("Event posted: %s → %s", event_type, eid)
            return eid
        log.warning("Intake returned %d for %s", resp.status_code, event_type)
    except Exception as e:
        log.warning("Failed to post event %s: %s", event_type, e)
    return None
