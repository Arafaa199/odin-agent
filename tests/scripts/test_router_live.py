#!/usr/bin/env python3
"""Live test — sends real requests to OpenRouter free models.

Run from the project root (excluded from the default pytest run):
    uv run python -m tests.scripts.test_router_live

Requires OPENROUTER_API_KEY in environment.
"""

from __future__ import annotations

import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.llm.models import FREE_MODELS
from src.llm.openrouter_client import OpenRouterClient, OpenRouterError, OpenRouterResponse
from src.llm.router import LLMRouter


def _divider(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def test_single_model_chat() -> None:
    """Test a single OpenRouter free model directly."""
    _divider("Test 1: Single Model Direct Chat")

    client = OpenRouterClient()
    result = client.chat(
        model=FREE_MODELS[0].id,
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=50,
    )

    if isinstance(result, OpenRouterResponse):
        print(f"  OK — model={result.model}")
        print(f"  Response: {result.text[:100]}")
        print(f"  Tokens: in={result.tokens_input} out={result.tokens_output}")
        print(f"  Latency: {result.latency_ms}ms")
    elif isinstance(result, OpenRouterError):
        print(f"  FAIL — {result.error_type}: {result.message}")
        if result.is_rate_limit:
            print(f"  Rate limited (retry after {result.retry_after}s)")


def test_router_simple() -> None:
    """Test the router with a simple prompt."""
    _divider("Test 2: Router Simple Prompt")

    router = LLMRouter()
    result = router.run(
        "What is 2 + 2? Reply with just the number.",
        tags={"confirm"},
        max_tokens=50,
    )

    print(f"  Backend: {result.backend}")
    print(f"  Model: {result.model}")
    print(f"  Response: {result.text[:100]}")
    print(f"  Attempts: {result.attempts}")
    print(f"  Complexity: {result.complexity}")


def test_router_with_system_prompt() -> None:
    """Test the router with a system prompt."""
    _divider("Test 3: Router with System Prompt")

    router = LLMRouter()
    result = router.run(
        "What color is the sky?",
        system_prompt="You are a pirate. Always respond in pirate speak.",
        max_tokens=100,
    )

    print(f"  Backend: {result.backend}")
    print(f"  Model: {result.model}")
    print(f"  Response: {result.text[:200]}")


def test_all_models_reachable() -> None:
    """Probe every free model with a tiny prompt."""
    _divider("Test 4: Probe All Free Models")

    client = OpenRouterClient()
    for m in FREE_MODELS:
        result = client.chat(
            model=m.id,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=10,
        )
        if isinstance(result, OpenRouterResponse):
            print(f"  OK  {m.name:30s} ({result.latency_ms}ms)")
        else:
            status = "RL" if result.is_rate_limit else f"ERR:{result.status_code}"
            print(f"  {status:4s} {m.name:30s} — {result.message[:60]}")
        time.sleep(1)  # Be nice to free tier


def test_router_status() -> None:
    """Show router diagnostics."""
    _divider("Test 5: Router Status")

    router = LLMRouter()
    status = router.get_status()
    print(f"  Total models: {status['total_models']}")
    print(f"  Available: {status['available_models']}")
    print(f"  All exhausted: {status['all_exhausted']}")
    for m in status["models"]:
        avail = "OK" if m["available"] else f"CD:{m['cooldown_remaining_s']}s"
        print(f"    {avail:10s} {m['model']}")


def test_complexity_routing() -> None:
    """Show how different tasks score for complexity."""
    _divider("Test 6: Complexity Routing (dry run)")

    from src.llm.complexity import score_complexity

    cases = [
        ("Check dashboard status", {"confirm"}, "AUTO"),
        ("Send the follow-up email", {"email", "followup"}, "AUTO"),
        ("Deploy database migration to production", {"critical", "operator"}, "PROD_WRITE"),
        ("Run security audit on AWS instances", {"security", "operator"}, "PROD_READONLY"),
        ("Simple task with long prompt " + "x" * 9000, set(), "AUTO"),
    ]

    for prompt, tags, tier in cases:
        score = score_complexity(prompt[:80], tags, tier)
        route = "CLI" if score.needs_cli else "FREE"
        print(f"  [{route}] {score.score:.2f} — {prompt[:50]}... tags={tags} tier={tier}")


if __name__ == "__main__":
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set")
        print("Set it first: export OPENROUTER_API_KEY=...")
        sys.exit(1)

    print("LLM Router Live Test Suite")
    print(f"API Key: ...{os.environ['OPENROUTER_API_KEY'][-6:]}")
    print(f"Free models: {len(FREE_MODELS)}")

    tests = [
        test_complexity_routing,  # No API calls
        test_single_model_chat,
        test_router_simple,
        test_router_with_system_prompt,
        test_router_status,
    ]

    # Full model probe is slow — only run if requested
    if "--probe-all" in sys.argv:
        tests.append(test_all_models_reachable)

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            print(f"\n  EXCEPTION: {e}")

    print(f"\n{'=' * 60}")
    print("  Done. Run with --probe-all to test every model.")
    print(f"{'=' * 60}")
