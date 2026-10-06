"""Odin Pipeline Watchdog — tiered data freshness alerts.

Runs daily from a timer. Queries ops.check_data_freshness(), classifies
domains into severity tiers, and alerts based on aggregate severity:

Tiers:
  CRITICAL — always-on automated pipelines and every backup leg.
             Any single CRITICAL stale → immediate page.
  HIGH     — automated but intermittent (capture, calendar, recurring).
             3+ HIGH stale → digest page. Otherwise log-only.
  LOW      — user-driven / event-driven (habits, meals, approvals, etc.).
             Never pages on its own. Visible in the digest only.

Escalation: max 1 per domain, then silenced until recovery.
Recovery notices always sent (any tier).

Usage: python -m src.watchdog
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import comms
from .config import load_settings
from .infra import db

log = logging.getLogger("watchdog")

STATE_PATH = Path(
    os.environ.get("WATCHDOG_STATE_PATH", "~/.odin/.watchdog-state.json")
).expanduser()

# Domain -> where to look when it goes stale. Domain names come from the
# database's ops.check_data_freshness(); unknown domains still alert, with
# less context.
PIPELINE_MAP = {
    "finance": {
        "pipeline": "Bank SMS transaction import",
        "services": ["transaction-import (laptop, launchd)"],
        "check": "launchctl list | grep transaction-import",
    },
    "health": {
        "pipeline": "Wearable fitness sync",
        "services": ["fitness-sync workflow (edge, n8n)"],
        "check": "ssh edge 'docker logs n8n --tail 20 2>&1 | grep -i fitness'",
    },
    "email": {
        "pipeline": "Email intelligence",
        "services": ["mail-intel (laptop, launchd)"],
        "check": "launchctl list | grep mail-intel",
    },
    "events": {
        "pipeline": "Core event intake",
        "services": ["intake.service (db-host)", "stall-recovery.timer (db-host)"],
        "check": "systemctl --user status intake stall-recovery --no-pager",
    },
    "dashboard": {
        "pipeline": "Daily facts refresh",
        "services": ["n8n nightly-maintenance (edge)"],
        "check": "ssh edge 'docker logs n8n --tail 20 2>&1 | grep -i nightly'",
    },
    "capture": {
        "pipeline": "Continuous capture sync",
        "services": ["capture-sync.timer (worker)"],
        "check": "systemctl --user status capture-sync",
    },
    "calendar": {
        "pipeline": "Mobile app calendar sync",
        "services": ["mobile app calendar sync", "n8n calendar webhook"],
        "check": "Check the mobile app's calendar sync",
    },
    "recurring": {
        "pipeline": "Recurring items / subscriptions",
        "services": ["n8n nightly maintenance"],
        "check": "Runs in nightly maintenance — check nightly logs",
    },
    "meals": {
        "pipeline": "Meal log",
        "services": ["mobile app meal log via intake"],
        "check": "User-driven — check if anything was logged today",
    },
    "habits": {
        "pipeline": "Habit completions",
        "services": ["mobile app habits view"],
        "check": "User-driven — staleness expected if no habit logs",
    },
    "approvals": {
        "pipeline": "Agent approval events",
        "services": ["task runner approval flow", "mobile app approvals card"],
        "check": "Expected stale if no approval requests",
    },
    "memory": {
        "pipeline": "Unified agent memory",
        "services": ["intake API /v1/memory/*"],
        "check": "Check agent activity — stale if no agents ran",
    },
    "agent_traces": {
        "pipeline": "Agent execution traces",
        "services": ["task runner and other agents"],
        "check": "Stale if no agents executed",
    },
    "utility_bills": {
        "pipeline": "Utility bill capture (email-triggered)",
        "services": ["mail-intel (laptop) -> utility-capture.service (worker)"],
        "check": "Check mail-intel logs for the utility-bill trigger",
    },
    "nas_backup": {
        "pipeline": "NAS nightly mirror to a second NAS",
        "services": [
            "nas-backup.sh (NAS task scheduler)",
            "nas-backup-freshness.timer (db-host, stamps this domain on success only)",
        ],
        "check": "ssh nas 'cat /var/log/nas-backup.status' — then tail the mirror's backup.log",
    },
    "edge_backup": {
        "pipeline": "edge nightly backup (restic + rsync mirror to NAS)",
        "services": [
            "edge-backup.timer (edge, systemd --user)",
            "edge-backup-freshness.timer (db-host, stamps this domain on success only)",
        ],
        "check": "ssh edge 'cat ~/backup-state/status-backup.json; "
        "systemctl --user status edge-backup'",
    },
    "worker_backup": {
        "pipeline": "worker nightly backup (restic sftp repo + rsync mirror)",
        "services": [
            "worker-backup.timer (worker, systemd --user)",
            "worker-backup-freshness.timer (db-host, stamps this domain on success only)",
        ],
        "check": "ssh worker 'cat ~/backup-state/status-backup.json; systemctl --user "
        "status worker-backup' — check the sftp target alias first",
    },
    "laptop_backup": {
        "pipeline": "laptop Time Machine to NAS",
        "services": [
            "Time Machine (laptop, macOS)",
            "laptop-backup-freshness.timer (db-host, stamps from the backup bundle's own mtime)",
        ],
        "check": "tmutil status / tmutil destinationinfo on the laptop",
    },
    "db_backup": {
        "pipeline": "db-host pg_dump (2x/day) → NAS mirror",
        "services": [
            "pg-backup.timer + backup-mirror.timer (db-host, systemd --user)",
            "db-backup-freshness.timer (db-host, stamps from the NAS artifact, >1MB only)",
        ],
        "check": "ls -lht on the NAS mirror — a tiny file is the empty-gzip failure mode",
    },
}

TIER_CRITICAL = "critical"
TIER_HIGH = "high"
TIER_LOW = "low"

DOMAIN_TIERS = {
    "health": TIER_CRITICAL,
    "dashboard": TIER_CRITICAL,
    "events": TIER_CRITICAL,
    # finance and email were HIGH. A lone stale HIGH is swallowed by the
    # 3-at-once threshold, and a single silent pipeline (bank SMS format drift)
    # meant months of missed data. A pipeline that must page alone is CRITICAL.
    "finance": TIER_CRITICAL,
    "email": TIER_CRITICAL,
    "capture": TIER_HIGH,
    "calendar": TIER_HIGH,
    "recurring": TIER_HIGH,
    "meals": TIER_LOW,
    "habits": TIER_LOW,
    "approvals": TIER_LOW,
    "memory": TIER_LOW,
    "agent_traces": TIER_LOW,
    "utility_bills": TIER_LOW,
    # Backup legs. Each domain is stamped only on a SUCCESSFUL backup, so going
    # stale means the backup failed, the target is off, or the scheduler never
    # fired.
    #
    # CRITICAL, not HIGH, and that distinction is the point. HIGH only pages once
    # HIGH_ALERT_THRESHOLD (3) domains are stale together, so a stale backup at
    # HIGH reaches a human ONLY IF two unrelated pipelines also broke. That was
    # observed in practice: the backup was detected as stale and then logged as
    # "Below Telegram threshold". A backup that has not succeeded must page alone.
    #
    # Each row exists because that leg once failed silently for weeks: a backup
    # script that failed open for months, a dump chain that shipped empty gzips,
    # a "second destination" that turned out to be an empty share.
    "nas_backup": TIER_CRITICAL,
    "edge_backup": TIER_CRITICAL,
    "worker_backup": TIER_CRITICAL,
    "laptop_backup": TIER_CRITICAL,
    "db_backup": TIER_CRITICAL,
}

ESCALATION_MULTIPLIER = 3.0
HIGH_ALERT_THRESHOLD = 3


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt watchdog state — starting fresh")
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _query_freshness() -> dict | None:
    if not db.is_configured():
        log.error("ODIN_DB_PASSWORD not set — cannot check freshness")
        return None
    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT ops.check_data_freshness()")
            row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return None
    except Exception as e:
        log.error("Failed to query freshness: %s", e)
        return None


APPROVAL_STALE_HOURS = 4
APPROVAL_EXPIRE_HOURS = 24
AGENT_DIR = Path(
    os.path.expanduser(os.environ.get("ODIN_AGENT_DIR", "~/.local/state/odin/agent"))
)


def _expire_approval(event_id: str, task_uuid: str) -> bool:
    """POST agent.approval_resolved (action=expired) to intake.

    Moved here when a separate healthcheck job was retired. This is the ONLY
    mechanism that expires abandoned approval requests; without it, unresolved
    approvals accumulate on the mobile app's approvals card forever."""
    import requests

    intake_url = os.environ.get("INTAKE_URL", "http://localhost:8100")
    intake_key = os.environ.get("INTAKE_KEY", "")
    if not intake_key:
        log.warning("INTAKE_KEY not set — cannot expire approval")
        return False
    ok = False
    try:
        resp = requests.post(
            f"{intake_url}/v1/events",
            json={
                "event_type": "agent.approval_resolved",
                "priority": "P1",
                "retention_class": "permanent",
                "idempotency_key": f"agent:resolved:{event_id}",
                "payload": {
                    "approval_event_id": event_id,
                    "task_uuid": task_uuid,
                    "action": "expired",
                    "resolved_by": "auto_timeout",
                },
            },
            headers={"X-Intake-Key": intake_key},
            timeout=5,
        )
        ok = resp.status_code in (200, 201)
        if ok:
            log.info("Auto-expired approval event %s", event_id[:8])
        else:
            log.warning("Intake returned %d for expire event", resp.status_code)
    except Exception as e:
        log.warning("Failed to expire approval: %s", e)

    pending_file = AGENT_DIR / ".pending-approval.json"
    if pending_file.exists():
        try:
            data = json.loads(pending_file.read_text())
            if data.get("task_uuid") == task_uuid:
                data["status"] = "expired"
                pending_file.write_text(json.dumps(data, indent=2))
        except Exception:
            pass
    return ok


def _sweep_stale_approvals() -> tuple[list[str], bool]:
    """Unresolved agent.approval_requested events: >24h auto-expire (an
    action — forces Telegram), >4h noted in the digest."""
    if not db.is_configured():
        return [], False
    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.event_id, e.payload, EXTRACT(EPOCH FROM (NOW() - e.created_at)) / 3600
                FROM core.events e
                WHERE e.event_type = 'agent.approval_requested'
                  AND e.status = 'pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM core.events r
                    WHERE r.event_type = 'agent.approval_resolved'
                      AND r.payload->>'approval_event_id' = e.event_id::text
                  )
                ORDER BY e.created_at DESC
            """)
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.warning("Failed to check stale approvals: %s", e)
        return [], False

    lines: list[str] = []
    acted = False
    for event_id, payload, age_h in rows:
        desc = (payload.get("task_description") or "")[:40]
        if age_h > APPROVAL_EXPIRE_HOURS:
            _expire_approval(str(event_id), payload.get("task_uuid", ""))
            acted = True
            lines.append(f"  Auto-expired approval ({age_h:.0f}h): {desc}")
        elif age_h > APPROVAL_STALE_HOURS:
            lines.append(f"  Stale approval ({age_h:.0f}h): {desc}")
    if rows:
        log.info(
            "Approval sweep: %d unresolved, %d flagged, expired=%s", len(rows), len(lines), acted
        )
    return lines, acted


def _check_sms_senders(state: dict) -> tuple[list[str], bool]:
    """Per-sender SMS staleness (finance.v_sms_sender_freshness).

    The aggregate `finance` freshness domain stays fresh as long as ANY bank's SMS
    still parses, so one bank's SILENT format drift is invisible (three months of
    missed spend once hid behind the other banks). The view flags a sender
    whose silence exceeds its own cadence (GREATEST(3× median inter-arrival gap,
    7d), ≥5 txns/180d to be eligible).

    Deduped like the domains via `state`: a newly-stale sender pages once (a silent
    bank is finance-critical → routes through the critical path), already-known ones
    ride along in the digest, and a recovery pages. Returns (digest_lines, should_page).
    """
    if not db.is_configured():
        return [], False
    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT account_id, account_name, n_txns_180d,
                       EXTRACT(EPOCH FROM age) / 86400.0 AS age_days, is_stale
                FROM finance.v_sms_sender_freshness
                ORDER BY account_id
                """
            )
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.warning("Failed to check SMS sender freshness: %s", e)
        return [], False

    tracked = state.setdefault("_sms_senders", {})
    lines: list[str] = []
    should_page = False
    stale_now: set[str] = set()

    for account_id, name, n_txns, age_days, is_stale in rows:
        if not is_stale:
            continue
        key = str(account_id)
        stale_now.add(key)
        lines.append(
            f"  *SILENT SMS*: `{name}` (acct {account_id}) — no parsed SMS in "
            f"{age_days:.0f}d ({n_txns} txns/180d). Check the live SMS format vs the regex."
        )
        if key not in tracked:
            tracked[key] = {"alerted_at": datetime.now(timezone.utc).isoformat()}
            should_page = True  # newly-silent sender — page once

    for key in list(tracked.keys()):
        if key not in stale_now:
            lines.append(f"  *SMS RECOVERED*: account {key} — parsing again")
            del tracked[key]
            should_page = True

    if lines:
        log.info("SMS sender sweep: %d stale, page=%s", len(stale_now), should_page)
    return lines, should_page


SEARCH_WORKER_UNIT = os.environ.get("SEARCH_WORKER_UNIT", "search-worker.service")
SEARCH_STORM_FRAC = 0.5  # a run re-embedding >=50% of a source's corpus = storm
SEARCH_STORM_MIN = 200  # ignore tiny sources (no meaningful "storm")
SEARCH_STARVATION_HOURS = 3  # 30-min cadence; >3h with no completed run = not draining
_JKEY2SRC = {
    "notes": "obsidian_note",
    "emails": "email",
    "receipts": "finance_receipt",
    "txns": "finance_transaction",
    "memories": "agent_memory",
    "traces": "agent_trace",
    "frames": "capture_frame",
    "audio": "capture_audio",
}


def _search_worker_last_run():
    """Parse the worker's last `[INFO] Processed: notes=..(..) ...` journal line
    (local journal; the watchdog runs on the worker host). Returns (run_ts, {source_type:
    items_reembedded}). The embeddings table cannot show re-embeds — updated_at is
    never bumped and ON CONFLICT DO UPDATE leaves both timestamps — and replicating
    the 8 md5 detection predicates here would be a third copy of the exact logic
    whose drift caused the storm, so the worker's own per-run summary is the source."""
    import re
    import subprocess

    try:
        out = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                SEARCH_WORKER_UNIT,
                "--since",
                "-6 hours",
                "-o",
                "short-iso",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except Exception as e:  # noqa: BLE001
        log.warning("search-worker journal read failed: %s", e)
        return None, {}
    last = None
    for line in out.splitlines():
        if "Processed:" in line:
            last = line
    if not last:
        return None, {}
    run_ts = None
    tok = last.split(None, 1)[0] if last.split() else ""
    try:
        run_ts = datetime.strptime(tok, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        run_ts = None
    processed = {}
    for k, v in re.findall(r"(\w+)=(\d+)\(\d+\)", last):
        src = _JKEY2SRC.get(k)
        if src:
            processed[src] = int(v)
    return run_ts, processed


def _check_search_worker(state: dict) -> tuple[list[str], bool]:
    """Functional assertions for the embedding worker (the re-embed-storm class —
    the worker silently re-embedded ~12k rows every 30 min for weeks, pegging 7
    cores, because a content_hash drift made every row look un-embedded).

    STORM (pages): last run re-embedded >= SEARCH_STORM_FRAC of a source's corpus.
    STARVATION (digest only): no completed run in > SEARCH_STARVATION_HOURS AND an
    un-embedded backlog exists (rows draining = healthy). State-deduped; non-critical
    tier (digest) except a storm, which pages."""
    if not db.is_configured():
        return [], False
    totals, backlog = {}, None
    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT source_type, count(*) FROM search.embeddings GROUP BY source_type")
            totals = {r[0]: int(r[1]) for r in cur.fetchall()}
            # Existence-backlog (robust, no md5) for the two biggest sources — a
            # worker that stops draining shows a growing, stuck backlog here.
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM raw.notes_index n
                     WHERE n.removed_at IS NULL AND n.content IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM search.embeddings e
                           WHERE e.source_type='obsidian_note' AND e.source_id=n.relative_path AND e.chunk_index=0))
                + (SELECT count(*) FROM memory.entries m
                     WHERE NOT EXISTS (SELECT 1 FROM search.embeddings e
                           WHERE e.source_type='agent_memory' AND e.source_id=m.id::text AND e.chunk_index=0))
            """)
            backlog = int(cur.fetchone()[0])
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("search-worker DB query failed: %s", e)
        return [], False

    corpus = sum(totals.values())
    run_ts, processed = _search_worker_last_run()
    now = datetime.now(timezone.utc)
    tracked = state.setdefault("_search_worker", {})
    lines: list[str] = []
    should_page = False

    # STORM — re-embedding ~a whole source's corpus in one run.
    storm = [
        f"{src} {n}/{totals[src]}"
        for src, n in processed.items()
        if totals.get(src, 0) >= SEARCH_STORM_MIN and n >= SEARCH_STORM_FRAC * totals[src]
    ]
    if storm:
        lines.append(
            "  *SEARCH RE-EMBED STORM*: last run re-embedded ~all of "
            + ", ".join(storm)
            + " — content_hash drift, check detection md5 vs stored content_hash"
        )
        if not tracked.get("storm"):
            tracked["storm"] = now.isoformat()
            should_page = True
    elif tracked.pop("storm", None):
        lines.append("  *SEARCH STORM RECOVERED*: re-embed churn back to normal")
        should_page = True

    # STARVATION — worker not draining the backlog (digest only).
    stale_run = run_ts is None or (now - run_ts).total_seconds() / 3600 > SEARCH_STARVATION_HOURS
    if stale_run and backlog:
        age = "unknown" if run_ts is None else f"{(now - run_ts).total_seconds() / 3600:.1f}h"
        lines.append(
            f"  *SEARCH WORKER STARVATION*: last completed run {age} ago, "
            f"{backlog} rows un-embedded and not draining (30-min cadence)"
        )
        tracked.setdefault("starved", now.isoformat())
    else:
        tracked.pop("starved", None)

    log.info(
        "search-worker: corpus=%d backlog=%s last_run=%s storm=%s",
        corpus,
        backlog,
        run_ts.isoformat() if run_ts else None,
        bool(storm),
    )
    return lines, should_page


def _format_stale_alert(domain: str, age_minutes: float, threshold: int) -> str:
    age_hours = age_minutes / 60
    threshold_hours = threshold / 60
    info = PIPELINE_MAP.get(domain, {})
    pipeline = info.get("pipeline", "Unknown pipeline")
    services = info.get("services", [])
    check = info.get("check", "No troubleshooting info")

    lines = [
        f"  *STALE*: `{domain}` ({age_hours:.0f}h, threshold {threshold_hours:.0f}h)",
        f"    Pipeline: {pipeline}",
    ]
    if services:
        lines.append(f"    Services: {', '.join(services)}")
    lines.append(f"    Check: `{check[:120]}`")
    return "\n".join(lines)


def _format_recovery(domain: str) -> str:
    info = PIPELINE_MAP.get(domain, {})
    pipeline = info.get("pipeline", domain)
    return f"  *RECOVERED*: `{domain}` — {pipeline} is fresh again"


def _format_escalation(domain: str, age_minutes: float, threshold: int) -> str:
    age_hours = age_minutes / 60
    info = PIPELINE_MAP.get(domain, {})
    pipeline = info.get("pipeline", domain)
    return (
        f"  *CRITICAL*: `{domain}` ({age_hours:.0f}h — "
        f"{age_minutes / threshold:.1f}x threshold)\n"
        f"    Pipeline: {pipeline} — may need manual intervention"
    )


def _get_tier(domain: str) -> str:
    return DOMAIN_TIERS.get(domain, TIER_LOW)


def _ping_deadman() -> None:
    """Ping an EXTERNAL healthcheck so a dead alert leg, or a watchdog that
    silently stops running, pages from outside the estate. Runs on every
    completed check, healthy or not. Fail-open: unset WATCHDOG_HC_URL skips it.
    WATCHDOG_HC_CA points at a private CA bundle if the endpoint needs one."""
    hc_url = os.environ.get("WATCHDOG_HC_URL", "").strip()
    if not hc_url:
        return
    try:
        import ssl

        ca = os.environ.get("WATCHDOG_HC_CA", "")
        ctx = ssl.create_default_context(cafile=ca) if ca else None
        urllib.request.urlopen(hc_url, timeout=10, context=ctx)
    except Exception as e:  # noqa: BLE001 — the ping must never break the run
        log.warning("alert-rail HC ping failed (non-fatal): %s", type(e).__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    comms.init_transports(load_settings())

    freshness = _query_freshness()
    if not freshness:
        log.error("Could not retrieve freshness data")
        if comms.available():
            comms.send(
                "Watchdog: Failed to query data freshness — DB may be unreachable", deadman=True
            )
        return

    domains = freshness.get("domains", [])
    stale_count = freshness.get("stale_count", 0)
    state = _load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    critical_alerts = []
    high_alerts = []
    low_alerts = []
    recoveries = []
    escalations_by_tier = {TIER_CRITICAL: [], TIER_HIGH: []}

    for d in domains:
        domain = d["domain"]
        is_stale = d["is_stale"]
        age_minutes = d["age_minutes"]
        threshold = d["threshold_minutes"]
        prev = state.get(domain, {})
        tier = _get_tier(domain)

        if is_stale:
            if not prev.get("alerted_at"):
                alert_text = _format_stale_alert(domain, age_minutes, threshold)
                state[domain] = {
                    "alerted_at": now_iso,
                    "age_at_alert": age_minutes,
                    "threshold_minutes": threshold,
                    "resolved_at": None,
                    "escalated_at": None,
                    "tier": tier,
                }
                if tier == TIER_CRITICAL:
                    critical_alerts.append(alert_text)
                elif tier == TIER_HIGH:
                    high_alerts.append(alert_text)
                else:
                    low_alerts.append(alert_text)
            elif (
                not prev.get("escalated_at")
                and age_minutes > threshold * ESCALATION_MULTIPLIER
                and tier != TIER_LOW
            ):
                esc_text = _format_escalation(domain, age_minutes, threshold)
                if tier == TIER_CRITICAL:
                    escalations_by_tier[TIER_CRITICAL].append(esc_text)
                else:
                    escalations_by_tier[TIER_HIGH].append(esc_text)
                state[domain]["escalated_at"] = now_iso
        else:
            if prev.get("alerted_at") and not prev.get("resolved_at"):
                recoveries.append(_format_recovery(domain))
                state[domain]["resolved_at"] = now_iso

    for domain in list(state.keys()):
        resolved_at = state[domain].get("resolved_at")
        if resolved_at:
            try:
                resolved_dt = datetime.fromisoformat(resolved_at)
                if resolved_dt.tzinfo is None:
                    resolved_dt = resolved_dt.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - resolved_dt).total_seconds() / 3600
                if age_h > 24:
                    del state[domain]
            except (ValueError, TypeError):
                del state[domain]

    # Per-sender SMS freshness: mutates `state` (dedup), so run it BEFORE
    # persisting. A silent bank is finance-critical -> routes through the critical path.
    sms_lines, sms_should_page = _check_sms_senders(state)

    # Search-worker functional assertions (re-embed storm / starvation). Mutates
    # `state` (dedup) -> before persist. Storm pages; starvation is digest-only.
    sw_lines, sw_should_page = _check_search_worker(state)

    _save_state(state)

    # Stale-approval sweep (moved from a retired healthcheck job): auto-expiry
    # is an intervention -> always pages; >4h items ride along in the digest.
    approval_lines, approval_acted = _sweep_stale_approvals()

    crit_esc = escalations_by_tier[TIER_CRITICAL]
    high_esc = escalations_by_tier[TIER_HIGH]

    has_critical = bool(critical_alerts) or bool(crit_esc) or sms_should_page
    high_count = len(high_alerts) + len(high_esc)
    has_high_batch = high_count >= HIGH_ALERT_THRESHOLD

    should_telegram = (
        has_critical or has_high_batch or bool(recoveries) or approval_acted or sw_should_page
    )

    _ping_deadman()

    if (
        not critical_alerts
        and not high_alerts
        and not low_alerts
        and not recoveries
        and not crit_esc
        and not high_esc
        and not approval_lines
        and not sms_lines
        and not sw_lines
    ):
        log.info("Watchdog: all monitored domains within thresholds")
        return

    lines = [f"*Odin Watchdog* — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]

    if crit_esc:
        lines.append("")
        lines.extend(crit_esc)

    if critical_alerts:
        lines.append("")
        lines.extend(critical_alerts)

    if high_esc:
        lines.append("")
        lines.extend(high_esc)

    if high_alerts:
        lines.append("")
        lines.extend(high_alerts)

    if low_alerts:
        lines.append("")
        lines.append("_Low-priority (log only):_")
        lines.extend(low_alerts)

    if recoveries:
        lines.append("")
        lines.extend(recoveries)

    if approval_lines:
        lines.append("")
        lines.append("_Approvals:_")
        lines.extend(approval_lines)

    if sms_lines:
        lines.append("")
        lines.append("_Per-sender SMS freshness:_")
        lines.extend(sms_lines)

    if sw_lines:
        lines.append("")
        lines.append("_Search embedding worker:_")
        lines.extend(sw_lines)

    lines.append(f"\n_{stale_count} domain(s) currently stale_")
    msg = "\n".join(lines)
    log.info(msg)

    if not should_telegram:
        log.info("Below Telegram threshold — logged only")
        return

    if has_critical and comms.available():
        results = comms.send_all(msg, deadman=True)
        log.info("Critical alert fan-out: %s", results)
    elif comms.send(msg, deadman=True):
        log.info("Alert sent via transport")
    else:
        log.warning("All transports unavailable — alert logged only")


if __name__ == "__main__":
    main()
