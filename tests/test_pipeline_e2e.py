#!/usr/bin/env python3
"""
End-to-end pipeline test with synthetic transcript.
Exercises: date resolver → validator → idem store → task creation (dry-run or real).

Usage:
    python -m tests.test_pipeline_e2e              # dry-run (no TW, no Ollama)
    python -m tests.test_pipeline_e2e --live-llm   # real Ollama extraction
    python -m tests.test_pipeline_e2e --live-tw    # real local TaskWarrior
    python -m tests.test_pipeline_e2e --live       # both
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# The audit log fails closed without a key; the dry run signs a throwaway temp log.
os.environ.setdefault("ODIN_AUDIT_KEY", "test-only-audit-key")

from src.ingest.schema import ActionItem, TranscriptSegment
from src.ingest.date_resolver import resolve, confidence_for
from src.ingest.validator import validate_items, verify_quote
from src.infra.idem_store import IdemStore
from src.infra import audit

# --- Synthetic test data ---

FAKE_RECORDING_DATE = "2026-02-15"
FAKE_RECORDING_NAME = "2026-02-15_test-pipeline_deadbeef.mp3"
FAKE_RECORDING_ID = hashlib.sha256(FAKE_RECORDING_NAME.encode()).hexdigest()[:12]

SYNTHETIC_SEGMENTS = [
    TranscriptSegment(
        start=0, end=5, text="Alright, let's go through the action items from today's meeting."
    ),
    TranscriptSegment(
        start=5,
        end=15,
        text="First, I need to update the deployment script by Friday. That's critical for the release.",
    ),
    TranscriptSegment(
        start=15,
        end=25,
        text="Second, Alex should review the pull request for the API changes. He said he'll get to it next week.",
    ),
    TranscriptSegment(
        start=25,
        end=35,
        text="Third, we decided to switch from REST to GraphQL for the new endpoints. That's final.",
    ),
    TranscriptSegment(
        start=35,
        end=45,
        text="Also, I'm not sure if we need to update the documentation. Maybe someone should look into that.",
    ),
    TranscriptSegment(
        start=45,
        end=55,
        text="Oh and one more thing — the staging server SSL cert expires end of month. I need to renew it.",
    ),
    TranscriptSegment(
        start=55,
        end=65,
        text="Quick question — has anyone checked if the backup cron is actually running on the new server?",
    ),
]

SYNTHETIC_FULL_TEXT = " ".join(s.text for s in SYNTHETIC_SEGMENTS)

# What the LLM _should_ extract (we simulate this for dry-run)
SIMULATED_LLM_OUTPUT = {
    "summary": "Team meeting covering deployment script update, API PR review, GraphQL migration decision, and infrastructure maintenance tasks.",
    "participants": ["Unknown", "Alex"],
    "action_items": [
        {
            "description": "Update the deployment script for the release",
            "assignee": "self",
            "urgency": "high",
            "confidence": 0.95,
            "project": "demo",
            "due_raw": "by Friday",
            "item_type": "task",
            "source_quote": "I need to update the deployment script by Friday",
            "source_time_start": "0:05",
            "source_time_end": "0:15",
            "tags": ["deployment", "release"],
        },
        {
            "description": "Review the pull request for the API changes",
            "assignee": "other",
            "urgency": "medium",
            "confidence": 0.85,
            "project": None,
            "due_raw": "next week",
            "item_type": "task",
            "source_quote": "Alex should review the pull request for the API changes",
            "source_time_start": "0:15",
            "source_time_end": "0:25",
            "tags": ["review"],
        },
        {
            "description": "Switch from REST to GraphQL for new endpoints",
            "assignee": "self",
            "urgency": "medium",
            "confidence": 0.9,
            "project": None,
            "due_raw": None,
            "item_type": "decision",
            "source_quote": "we decided to switch from REST to GraphQL for the new endpoints",
            "source_time_start": "0:25",
            "source_time_end": "0:35",
            "tags": ["architecture"],
        },
        {
            "description": "Update the documentation",
            "assignee": "unknown",
            "urgency": "low",
            "confidence": 0.4,
            "project": None,
            "due_raw": None,
            "item_type": "task",
            "source_quote": "I'm not sure if we need to update the documentation",
            "source_time_start": "0:35",
            "source_time_end": "0:45",
            "tags": ["docs"],
        },
        {
            "description": "Renew staging server SSL certificate",
            "assignee": "self",
            "urgency": "high",
            "confidence": 0.92,
            "project": None,
            "due_raw": "end of month",
            "item_type": "task",
            "source_quote": "the staging server SSL cert expires end of month. I need to renew it",
            "source_time_start": "0:45",
            "source_time_end": "0:55",
            "tags": ["infra", "ssl"],
        },
        {
            "description": "Check if the backup cron is running on the new server",
            "assignee": "self",
            "urgency": "medium",
            "confidence": 0.6,
            "project": None,
            "due_raw": None,
            "item_type": "question",
            "source_quote": "has anyone checked if the backup cron is actually running",
            "source_time_start": "0:55",
            "source_time_end": "1:05",
            "tags": ["infra", "backup"],
        },
        {
            "description": "Ghost task with no source evidence",
            "assignee": "self",
            "urgency": "medium",
            "confidence": 0.7,
            "project": None,
            "due_raw": None,
            "item_type": "task",
            "source_quote": "this phrase does not exist anywhere in the transcript at all",
            "source_time_start": "0:00",
            "source_time_end": "0:05",
            "tags": [],
        },
    ],
    "key_decisions": ["Switch from REST to GraphQL for new endpoints"],
}


def run_test(live_llm: bool = False, live_tw: bool = False):
    print("=" * 70)
    print("PIPELINE E2E TEST")
    print(f"  Recording: {FAKE_RECORDING_NAME}")
    print(f"  Date: {FAKE_RECORDING_DATE}")
    print(f"  Recording ID: {FAKE_RECORDING_ID}")
    print(
        f"  Mode: {'LIVE' if (live_llm or live_tw) else 'DRY-RUN'} "
        f"(LLM={'live' if live_llm else 'simulated'}, TW={'live' if live_tw else 'dry-run'})"
    )
    print("=" * 70)

    # --- Stage 1: Transcription (always simulated) ---
    print("\n[STAGE 1] Transcription (simulated)")
    print(f"  Segments: {len(SYNTHETIC_SEGMENTS)}")
    print(f"  Full text: {len(SYNTHETIC_FULL_TEXT)} chars")
    transcript_checksum = hashlib.sha256(SYNTHETIC_FULL_TEXT.encode()).hexdigest()[:16]
    print(f"  Checksum: {transcript_checksum}")

    # --- Stage 2: LLM Extraction ---
    if live_llm:
        print("\n[STAGE 2] LLM Extraction (LIVE — calling Ollama)")
        from src.ingest.plaud import extract_actions

        extracted = extract_actions(
            SYNTHETIC_FULL_TEXT,
            segments=SYNTHETIC_SEGMENTS,
        )
    else:
        print("\n[STAGE 2] LLM Extraction (simulated)")
        extracted = SIMULATED_LLM_OUTPUT

    raw_items = extracted.get("action_items", [])
    print(f"  Summary: {extracted.get('summary', '')[:100]}")
    print(f"  Raw items: {len(raw_items)}")

    # --- Stage 3: Date resolution ---
    print("\n[STAGE 3] Date Resolution")
    action_items = []
    for a in raw_items:
        if not a.get("description"):
            continue

        due_raw = a.get("due_raw")
        resolved_date, was_inferred = resolve(due_raw, FAKE_RECORDING_DATE)
        due_conf = confidence_for(due_raw)

        item = ActionItem(
            description=a.get("description", ""),
            assignee=a.get("assignee", "self"),
            urgency=a.get("urgency", "medium"),
            confidence=a.get("confidence", 0.0),
            project=a.get("project"),
            due_date=resolved_date,
            due_raw=due_raw,
            due_confidence=due_conf,
            inferred_due=was_inferred,
            tags=a.get("tags", []),
            item_type=a.get("item_type", "task"),
            source_quote=a.get("source_quote", ""),
            source_time_start=a.get("source_time_start", ""),
            source_time_end=a.get("source_time_end", ""),
        )
        action_items.append(item)

        date_info = f"{resolved_date}" if resolved_date else "None"
        if was_inferred:
            date_info += " (inferred)"
        print(
            f"  [{item.item_type:8s}] {item.description[:50]:50s} | due_raw={due_raw!s:20s} → {date_info} (conf={due_conf})"
        )

    # --- Stage 4: Validation ---
    print("\n[STAGE 4] Validation")
    accepted, rejected = validate_items(action_items, SYNTHETIC_FULL_TEXT)

    print(f"  Input: {len(action_items)} items")
    print(f"  Accepted: {len(accepted)}")
    print(f"  Rejected: {len(rejected)}")

    for item in accepted:
        flags = []
        if item.requires_confirmation:
            flags.append(f"CONFIRM: {item.confirmation_question}")
        overlap = verify_quote(item.source_quote, SYNTHETIC_FULL_TEXT)
        print(
            f"  [OK ] {item.description[:55]:55s} | quote_overlap={overlap:.0%} | assignee={item.assignee}"
        )
        for f in flags:
            print(f"         {f}")

    for r in rejected:
        print(
            f"  [REJ] {r['item'][:55]:55s} | reason={r['reason']} overlap={r.get('overlap', 'n/a')}"
        )

    # --- Stage 5: Idempotency ---
    print("\n[STAGE 5] Idempotency")
    with tempfile.TemporaryDirectory() as td:
        idem = IdemStore(Path(td) / "test_idem.db")

        # First pass
        created_first = 0
        deduped_first = 0
        for item in accepted:
            key = item.idempotency_key(FAKE_RECORDING_ID)
            if idem.exists(key):
                deduped_first += 1
                print(f"  [DUP] {item.description[:50]} (key={key})")
            else:
                idem.record(key, FAKE_RECORDING_ID, item.description[:200])
                created_first += 1
                print(f"  [NEW] {item.description[:50]} (key={key})")

        # Second pass (should all be dupes)
        print("\n  --- Second pass (re-run simulation) ---")
        created_second = 0
        deduped_second = 0
        for item in accepted:
            key = item.idempotency_key(FAKE_RECORDING_ID)
            if idem.exists(key):
                deduped_second += 1
                print(f"  [DUP] {item.description[:50]} (key={key})")
            else:
                created_second += 1
                print(f"  [NEW] {item.description[:50]} (key={key})")

        idem.close()

    print(f"\n  First pass:  {created_first} new, {deduped_first} deduped")
    print(f"  Second pass: {created_second} new, {deduped_second} deduped")

    idem_ok = created_second == 0 and deduped_second == created_first
    print(f"  Idempotency: {'PASS' if idem_ok else 'FAIL'}")

    # --- Stage 6: Task creation ---
    print("\n[STAGE 6] Task Creation")
    self_tasks = [i for i in accepted if i.assignee == "self" and i.confidence >= 0.7]
    non_self = [i for i in accepted if i.assignee != "self"]
    low_conf = [i for i in accepted if i.confidence < 0.7]
    confirm = [i for i in accepted if i.requires_confirmation]

    print(f"  Self tasks (would create):     {len(self_tasks)}")
    for item in self_tasks:
        ", ".join(item.tags) if item.tags else "none"
        confirm_flag = " [NEEDS CONFIRM]" if item.requires_confirmation else ""
        print(
            f"    - {item.description[:60]} | due={item.due_date} | pri={item.urgency}{confirm_flag}"
        )

    print(f"  Non-self (logged only):        {len(non_self)}")
    for item in non_self:
        print(f"    - @{item.assignee}: {item.description[:60]}")

    print(f"  Low confidence (skipped):      {len(low_conf)}")
    for item in low_conf:
        print(f"    - ({item.confidence:.0%}) {item.description[:60]}")

    print(f"  Requires confirmation:         {len(confirm)}")

    if live_tw and self_tasks:
        print("\n  --- LIVE TaskWarrior creation ---")
        from src.actions.taskwarrior import create_task

        with tempfile.TemporaryDirectory() as td:
            idem = IdemStore(Path(td) / "test_idem.db")
            for item in self_tasks:
                result = create_task(
                    item, FAKE_RECORDING_NAME, idem_store=idem, recording_id=FAKE_RECORDING_ID
                )
                status = "OK" if result["success"] else result.get("error", "FAIL")
                print(f"    [{status}] {item.description[:50]} uuid={result.get('uuid', 'n/a')}")
            idem.close()
    else:
        print("\n  (dry-run — no tasks created in TaskWarrior)")

    # --- Stage 7: Audit log ---
    print("\n[STAGE 7] Audit Log")
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        audit_path = f.name

    audit.append(
        {
            "stage": "transcribe",
            "recording_id": FAKE_RECORDING_ID,
            "segments": len(SYNTHETIC_SEGMENTS),
        },
        log_path=audit_path,
    )
    audit.append(
        {"stage": "extract", "recording_id": FAKE_RECORDING_ID, "raw_count": len(raw_items)},
        log_path=audit_path,
    )
    audit.append(
        {
            "stage": "validate",
            "recording_id": FAKE_RECORDING_ID,
            "accepted": len(accepted),
            "rejected": len(rejected),
        },
        log_path=audit_path,
    )
    audit.append(
        {
            "stage": "create_tasks",
            "recording_id": FAKE_RECORDING_ID,
            "created": len(self_tasks),
            "deduped": 0,
        },
        log_path=audit_path,
    )

    events = audit.read_events(log_path=audit_path)
    print(f"  Events written: {len(events)}")
    for evt in events:
        print(
            f"    [{evt['stage']:12s}] {json.dumps({k: v for k, v in evt.items() if k not in ('_ts', '_iso', 'stage')})}"
        )

    Path(audit_path).unlink()

    # --- Summary ---
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)

    checks = {
        "Transcription": len(SYNTHETIC_SEGMENTS) > 0,
        "LLM extraction": len(raw_items) > 0,
        "Date resolution": any(i.due_date for i in accepted),
        "Validator (accept)": len(accepted) > 0,
        "Validator (reject)": len(rejected) > 0,
        "Confirmation flags": len(confirm) > 0,
        "Idempotency": idem_ok,
        "Non-self filtering": len(non_self) > 0,
        "Low-conf filtering": len(low_conf) > 0,
        "Audit log": len(events) == 4,
    }

    all_pass = True
    for name, passed in checks.items():
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}] {name}")
        if not passed:
            all_pass = False

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    return 0 if all_pass else 1


def test_pipeline_dry_run():
    assert run_test() == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="E2E pipeline test")
    parser.add_argument("--live-llm", action="store_true", help="Use real Ollama for extraction")
    parser.add_argument("--live-tw", action="store_true", help="Create real TaskWarrior tasks")
    parser.add_argument("--live", action="store_true", help="Both --live-llm and --live-tw")
    args = parser.parse_args()

    live_llm = args.live_llm or args.live
    live_tw = args.live_tw or args.live

    sys.exit(run_test(live_llm=live_llm, live_tw=live_tw))
