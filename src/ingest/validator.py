"""Post-LLM validation layer. Verifies source quotes, applies confirmation rules."""

from __future__ import annotations

import logging

from ..config import vocabulary
from .schema import ActionItem

log = logging.getLogger("validator")

QUOTE_MIN_OVERLAP = 0.6
CONFIDENCE_THRESHOLD = 0.7
DUE_CONFIDENCE_THRESHOLD = 0.5

VAGUE_BLOCKLIST = {
    "send an email", "send email", "follow up", "follow-up", "followup", "check in",
    "touch base", "circle back", "look into it", "sort it out", "chase it up",
    "make a call", "do it", "sort this", "handle it", "deal with it",
    "get back to them", "reach out", "continue recruitment process",
    "present the solution details",
}
# Project and system names from settings `vocabulary`: a short description that
# names one of these is concrete enough to keep.
_vocab = vocabulary()
VAGUE_REFERENT_VOCAB = frozenset(_vocab["projects"] + _vocab["systems"])


def verify_quote(quote: str, full_text: str) -> float:
    if not quote or not full_text:
        return 0.0
    if quote.lower() in full_text.lower():
        return 1.0
    quote_words = set(quote.lower().split())
    text_words = set(full_text.lower().split())
    if not quote_words:
        return 0.0
    overlap = len(quote_words & text_words) / len(quote_words)
    return overlap


def _has_referent(words: list[str]) -> bool:
    for w in words[1:]:
        cw = w.strip(".,:;'\"()")
        if any(c.isdigit() for c in cw):
            return True
        if len(cw) > 1 and (cw[0].isupper() or cw.isupper()):
            return True
    return any(w.lower().strip(".,:;'\"()") in VAGUE_REFERENT_VOCAB for w in words)


def is_too_vague(description: str) -> bool:
    """Reject context-free fragments. High-precision: keeps anything naming a
    person / number / system; drops only short generic phrasings. Checked against
    213 real recorded tasks: 0 false-positives on kept items."""
    d = (description or "").strip()
    dl = d.lower().rstrip(".!?")
    if not dl:
        return True
    if dl in VAGUE_BLOCKLIST:
        return True
    words = d.split()
    if len(words) < 3:
        return True
    if len(words) < 6 and not _has_referent(words):
        return True
    return False


def validate_items(items: list[ActionItem], full_text: str) -> tuple[list[ActionItem], list[dict]]:
    """Validate extracted items. Returns (accepted, rejected_with_reasons)."""
    accepted = []
    rejected = []

    for item in items:
        # Reject context-free / non-actionable fragments before anything else
        if is_too_vague(item.description):
            rejected.append({"item": item.description, "reason": "too_vague"})
            log.info(f"Rejected (too vague): {item.description[:60]}")
            continue

        # Check source quote exists and matches transcript
        if not item.source_quote:
            rejected.append({"item": item.description, "reason": "no_source_quote"})
            log.info(f"Rejected (no source quote): {item.description[:60]}")
            continue

        overlap = verify_quote(item.source_quote, full_text)
        if overlap < QUOTE_MIN_OVERLAP:
            rejected.append(
                {
                    "item": item.description,
                    "reason": "quote_not_found",
                    "overlap": round(overlap, 2),
                }
            )
            log.info(f"Rejected (quote overlap {overlap:.0%}): {item.description[:60]}")
            continue

        # Apply confirmation rules
        if item.confidence < CONFIDENCE_THRESHOLD:
            item.requires_confirmation = True
            item.confirmation_question = f"Low confidence ({item.confidence:.0%}): '{item.description}' — is this a real task?"

        if item.due_date and item.due_confidence < DUE_CONFIDENCE_THRESHOLD:
            item.requires_confirmation = True
            item.confirmation_question = (
                f"Uncertain date: '{item.description}' due {item.due_date} "
                f"(from '{item.due_raw}') — correct?"
            )

        if item.assignee == "unknown":
            item.requires_confirmation = True
            item.confirmation_question = (
                f"Unknown assignee: '{item.description}' — is this your task?"
            )

        if item.item_type == "question":
            item.requires_confirmation = True

        accepted.append(item)

    return accepted, rejected
