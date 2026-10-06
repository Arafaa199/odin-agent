"""Tests for src.llm.router — LLM routing with fallback chain."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from unittest.mock import MagicMock


from src.llm.models import FreeModel
from src.llm.openrouter_client import OpenRouterClient, OpenRouterError, OpenRouterResponse
from src.llm.router import LLMRouter, ModelCooldown


# ── Fixtures ─────────────────────────────────────────────────────────


@dataclass
class FakeRunResult:
    """Mimics claude_runner.RunResult for testing."""

    exit_code: int = 0
    output: str = "Claude CLI output"
    timed_out: bool = False
    duration_seconds: float = 1.5
    destructive_warnings: list[str] = field(default_factory=list)
    tokens_input: int = 100
    tokens_output: int = 50
    cost_usd: float = 0.0
    num_turns: int = 1


def _make_success_response(text: str = "Hello from free model") -> OpenRouterResponse:
    return OpenRouterResponse(
        text=text,
        model="test/free-model:free",
        tokens_input=10,
        tokens_output=20,
        finish_reason="stop",
        latency_ms=150,
    )


def _make_rate_limit_error(retry_after: float = 30.0) -> OpenRouterError:
    return OpenRouterError(
        status_code=429,
        error_type="rate_limit",
        message="Rate limited",
        is_rate_limit=True,
        retry_after=retry_after,
    )


TWO_MODELS = (
    FreeModel(id="test/model-a:free", name="Model A", context_window=100_000, strength="strong"),
    FreeModel(id="test/model-b:free", name="Model B", context_window=100_000, strength="medium"),
)

THREE_MODELS = (
    *TWO_MODELS,
    FreeModel(id="test/model-c:free", name="Model C", context_window=50_000, strength="light"),
)


# ── ModelCooldown ────────────────────────────────────────────────────


class TestModelCooldown:
    def test_initial_state_not_cooled(self) -> None:
        cd = ModelCooldown(model_id="test")
        assert not cd.is_cooled_down

    def test_apply_cooldown(self) -> None:
        cd = ModelCooldown(model_id="test")
        cd.apply_cooldown(60)
        assert cd.is_cooled_down
        assert cd.consecutive_failures == 1

    def test_reset_clears_cooldown(self) -> None:
        cd = ModelCooldown(model_id="test")
        cd.apply_cooldown(60)
        cd.reset()
        assert not cd.is_cooled_down
        assert cd.consecutive_failures == 0

    def test_cooldown_expires(self) -> None:
        cd = ModelCooldown(model_id="test")
        cd.apply_cooldown(0.01)
        time.sleep(0.02)
        assert not cd.is_cooled_down


# ── Router: Happy Path ──────────────────────────────────────────────


class TestRouterHappyPath:
    def test_simple_prompt_uses_first_free_model(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response("Model A says hi")

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        result = router.run("Hello", tags={"confirm"})

        assert result.backend == "openrouter"
        assert result.text == "Model A says hi"
        assert result.cost_usd == 0.0
        assert result.attempts == 1
        client.chat.assert_called_once()
        call_model = client.chat.call_args[1]["model"]  # keyword arg
        assert call_model == "test/model-a:free"

    def test_system_prompt_included(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response()

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        router.run("Hello", system_prompt="You are helpful")

        messages = client.chat.call_args[1]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful"

    def test_output_alias(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response("test output")

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        result = router.run("Hello")
        assert result.output == result.text == "test output"


# ── Router: Fallback Chain ──────────────────────────────────────────


class TestRouterFallback:
    def test_fallback_on_rate_limit(self) -> None:
        """When model A is rate-limited, falls back to model B."""
        client = MagicMock(spec=OpenRouterClient)
        client.chat.side_effect = [
            _make_rate_limit_error(),
            _make_success_response("Model B saves the day"),
        ]

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        result = router.run("Hello")

        assert result.backend == "openrouter"
        assert result.text == "Model B saves the day"
        assert result.attempts == 2
        assert client.chat.call_count == 2

    def test_all_models_rate_limited_falls_to_cli(self) -> None:
        """When all free models fail, falls back to Claude CLI."""
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_rate_limit_error()

        claude_fn = MagicMock(return_value=FakeRunResult(output="CLI to the rescue"))

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run("Hello", working_dir="/tmp")

        assert result.backend == "claude_cli"
        assert result.text == "CLI to the rescue"
        claude_fn.assert_called_once()

    def test_all_models_exhausted_no_cli_returns_error(self) -> None:
        """No CLI configured + all models exhausted = error."""
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_rate_limit_error()

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=None,
            models=TWO_MODELS,
        )
        result = router.run("Hello")

        assert result.backend == "error"
        assert "exhausted" in result.text.lower() or "ERROR" in result.text

    def test_skips_cooled_down_models(self) -> None:
        """Models in cooldown are skipped, next available is tried."""
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response("Model C works")

        router = LLMRouter(openrouter_client=client, models=THREE_MODELS, cooldown_seconds=300)
        # Manually cool down A and B
        router._get_cooldown("test/model-a:free").apply_cooldown(300)
        router._get_cooldown("test/model-b:free").apply_cooldown(300)

        result = router.run("Hello")

        assert result.text == "Model C works"
        call_model = client.chat.call_args[1]["model"]
        assert call_model == "test/model-c:free"

    def test_cooldown_resets_on_success(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response()

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        # Set a stale cooldown on model A (but expired)
        cd = router._get_cooldown("test/model-a:free")
        cd.consecutive_failures = 3
        cd.until = 0  # expired

        router.run("Hello")
        assert cd.consecutive_failures == 0


# ── Router: Complexity Routing ──────────────────────────────────────


class TestRouterComplexity:
    def test_complex_task_goes_to_cli(self) -> None:
        """Tasks with PROD_WRITE tier + complex tags skip free models."""
        client = MagicMock(spec=OpenRouterClient)
        claude_fn = MagicMock(return_value=FakeRunResult(output="CLI handles prod"))

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run(
            "Deploy database migration to production",
            tags={"critical"},
            tier="PROD_WRITE",
            working_dir="/tmp",
        )

        assert result.backend == "claude_cli"
        client.chat.assert_not_called()  # Free models NOT tried

    def test_simple_task_stays_on_free(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response()

        claude_fn = MagicMock()
        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run("Check dashboard", tags={"confirm"})

        assert result.backend == "openrouter"
        claude_fn.assert_not_called()

    def test_force_cli_bypasses_free(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        claude_fn = MagicMock(return_value=FakeRunResult())

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run("Simple task", force_cli=True, working_dir="/tmp")

        assert result.backend == "claude_cli"
        client.chat.assert_not_called()


# ── Router: CLI Fallback Details ────────────────────────────────────


class TestRouterCLIFallback:
    def test_cli_result_has_compat_fields(self) -> None:
        claude_fn = MagicMock(
            return_value=FakeRunResult(
                exit_code=0,
                output="done",
                timed_out=False,
                destructive_warnings=["rm -rf /"],
                tokens_input=100,
                tokens_output=50,
                num_turns=3,
            )
        )

        router = LLMRouter(
            openrouter_client=MagicMock(spec=OpenRouterClient),
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run("task", force_cli=True, working_dir="/tmp")

        assert result.exit_code == 0
        assert not result.timed_out
        assert result.destructive_warnings == ("rm -rf /",)
        assert result.num_turns == 3
        assert result.tokens_input == 100

    def test_cli_exception_returns_error_result(self) -> None:
        claude_fn = MagicMock(side_effect=RuntimeError("binary not found"))

        router = LLMRouter(
            openrouter_client=MagicMock(spec=OpenRouterClient),
            claude_runner_fn=claude_fn,
            models=TWO_MODELS,
        )
        result = router.run("task", force_cli=True, working_dir="/tmp")

        assert result.backend == "error"
        assert "binary not found" in result.text
        assert result.exit_code == 1


# ── Router: Status/Diagnostics ──────────────────────────────────────


class TestRouterStatus:
    def test_status_all_available(self) -> None:
        router = LLMRouter(
            openrouter_client=MagicMock(spec=OpenRouterClient),
            models=TWO_MODELS,
        )
        status = router.get_status()
        assert status["total_models"] == 2
        assert status["available_models"] == 2
        assert not status["all_exhausted"]

    def test_status_some_cooled_down(self) -> None:
        router = LLMRouter(
            openrouter_client=MagicMock(spec=OpenRouterClient),
            models=TWO_MODELS,
        )
        router._get_cooldown("test/model-a:free").apply_cooldown(300)
        status = router.get_status()
        assert status["available_models"] == 1
        assert not status["all_exhausted"]

    def test_status_all_exhausted(self) -> None:
        router = LLMRouter(
            openrouter_client=MagicMock(spec=OpenRouterClient),
            models=TWO_MODELS,
        )
        for m in TWO_MODELS:
            router._get_cooldown(m.id).apply_cooldown(300)
        status = router.get_status()
        assert status["all_exhausted"]


# ── Router: Exponential Backoff ─────────────────────────────────────


class TestRouterBackoff:
    def test_consecutive_failures_increase_cooldown(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        # Always rate limit
        client.chat.return_value = _make_rate_limit_error(retry_after=30)

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=None,
            models=(TWO_MODELS[0],),  # single model
            cooldown_seconds=60,
        )

        # First failure
        router.run("test1")
        cd = router._get_cooldown("test/model-a:free")

        # Reset for second attempt (simulate cooldown expired)
        cd.until = 0
        router.run("test2")

        # Second cooldown should be longer (1.5x exponential)
        assert cd.consecutive_failures == 2


# ── Router: Edge Cases ──────────────────────────────────────────────


class TestRouterEdgeCases:
    def test_empty_prompt(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = _make_success_response("ok")

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        result = router.run("")
        assert result.text == "ok"

    def test_no_models_configured_falls_to_cli(self) -> None:
        """With no free models, router falls through to Claude CLI."""
        client = MagicMock(spec=OpenRouterClient)
        claude_fn = MagicMock(return_value=FakeRunResult(output="CLI fallback"))

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=claude_fn,
            models=(),
        )
        result = router.run("Hello", working_dir="/tmp")
        assert result.backend == "claude_cli"
        assert result.text == "CLI fallback"
        client.chat.assert_not_called()

    def test_auth_error_long_cooldown(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.side_effect = [
            OpenRouterError(
                status_code=401,
                error_type="auth",
                message="bad key",
                is_auth_error=True,
            ),
            _make_success_response("model B ok"),
        ]

        router = LLMRouter(openrouter_client=client, models=TWO_MODELS)
        result = router.run("Hello")

        cd = router._get_cooldown("test/model-a:free")
        assert cd.until - time.monotonic() > 3500  # ~1h cooldown
        assert result.text == "model B ok"
