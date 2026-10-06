"""Complexity scoring — determines whether a task needs Claude CLI or can use free models."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ComplexityScore:
    score: float  # 0.0 (trivial) to 1.0 (very complex)
    reason: str
    needs_cli: bool  # True = route to Claude CLI


# Tags that indicate complex work requiring Claude CLI
COMPLEX_TAGS = frozenset({"critical", "operator", "security", "migration", "prod_write"})

# Tags that indicate simple triage/status work
SIMPLE_TAGS = frozenset({"confirm", "followup", "meeting", "email", "demo", "plaud"})

# Patterns in prompts suggesting multi-step or dangerous operations
COMPLEX_PATTERNS = re.compile(
    r"migration|deploy|rollback|database\s+schema|"
    r"security\s+audit|credential\s+rotation|"
    r"refactor|architect|re-?design|"
    r"multi.?step|end.?to.?end|"
    r"ssh.*&&.*ssh|"
    r"production|prod\s+",
    re.IGNORECASE,
)

# Minimum prompt length (chars) that suggests complexity
LONG_PROMPT_THRESHOLD = 8000

# Score threshold — above this → Claude CLI
CLI_THRESHOLD = 0.6


def score_complexity(
    prompt: str,
    tags: frozenset[str] | set[str] | None = None,
    tier: str = "AUTO",
) -> ComplexityScore:
    """Score a task's complexity to decide routing.

    Returns ComplexityScore with needs_cli=True if the task should
    go to Claude CLI instead of free OpenRouter models.
    """
    tags = tags or frozenset()
    score = 0.0
    reasons: list[str] = []

    # Tag-based scoring
    complex_matches = tags & COMPLEX_TAGS
    if complex_matches:
        score += 0.3 * len(complex_matches)
        reasons.append(f"complex tags: {', '.join(sorted(complex_matches))}")

    simple_matches = tags & SIMPLE_TAGS
    if simple_matches:
        score -= 0.15 * len(simple_matches)

    # Tier-based scoring
    if tier == "PROD_WRITE":
        score += 0.4
        reasons.append("PROD_WRITE tier")
    elif tier == "PROD_READONLY":
        score += 0.15

    # Prompt complexity patterns
    pattern_matches = COMPLEX_PATTERNS.findall(prompt)
    if pattern_matches:
        score += min(0.3, 0.1 * len(pattern_matches))
        reasons.append(f"complex patterns: {len(pattern_matches)} matches")

    # Prompt length as proxy for complexity
    if len(prompt) > LONG_PROMPT_THRESHOLD:
        score += 0.15
        reasons.append(f"long prompt: {len(prompt)} chars")

    score = max(0.0, min(1.0, score))
    needs_cli = score >= CLI_THRESHOLD

    reason = "; ".join(reasons) if reasons else "simple task"
    if needs_cli:
        reason = f"CLI required ({score:.2f}): {reason}"
    else:
        reason = f"free model OK ({score:.2f}): {reason}"

    return ComplexityScore(score=score, reason=reason, needs_cli=needs_cli)
