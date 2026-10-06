"""Integration tests for the LLM router — end-to-end scenarios.

These test the full flow without mocking the router internals,
only mocking the external HTTP calls and Claude CLI subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock


from src.llm.models import FREE_MODELS
from src.llm.openrouter_client import OpenRouterClient, OpenRouterError, OpenRouterResponse
from src.llm.router import LLMRouter


@dataclass
class FakeRunResult:
    exit_code: int = 0
    output: str = "CLI output"
    timed_out: bool = False
    duration_seconds: float = 2.0
    destructive_warnings: list[str] = field(default_factory=list)
    tokens_input: int = 200
    tokens_output: int = 100
    cost_usd: float = 0.0
    num_turns: int = 1


class TestFullFallbackChain:
    """Simulate a cascade through all models then to CLI."""

    def test_cascade_through_all_free_then_cli(self) -> None:
        """Rate-limit every free model → should reach Claude CLI."""
        rate_limit = OpenRouterError(
            status_code=429,
            error_type="rate_limit",
            message="Rate limited",
            is_rate_limit=True,
            retry_after=30,
        )

        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = rate_limit

        cli_fn = MagicMock(return_value=FakeRunResult(output="CLI handled it"))

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=cli_fn,
            models=FREE_MODELS,
        )
        result = router.run("Complex task", working_dir="/tmp")

        assert result.backend == "claude_cli"
        assert result.text == "CLI handled it"
        # Should have tried MAX_RETRIES_PER_REQUEST models before giving up
        assert client.chat.call_count <= 3  # MAX_RETRIES_PER_REQUEST

    def test_second_model_succeeds_after_first_fails(self) -> None:
        """First model fails, second succeeds — no CLI needed."""
        responses = [
            OpenRouterError(
                status_code=500,
                error_type="server_error",
                message="Model down",
            ),
            OpenRouterResponse(
                text="Second model works",
                model=FREE_MODELS[1].id,
                tokens_input=10,
                tokens_output=20,
                finish_reason="stop",
                latency_ms=200,
            ),
        ]

        client = MagicMock(spec=OpenRouterClient)
        client.chat.side_effect = responses

        cli_fn = MagicMock()
        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=cli_fn,
            models=FREE_MODELS[:3],
        )
        result = router.run("Task")

        assert result.backend == "openrouter"
        assert result.text == "Second model works"
        cli_fn.assert_not_called()


class TestComplexityRoutingIntegration:
    """Test that complexity scoring correctly routes tasks."""

    def test_critical_operator_security_goes_to_cli(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        cli_fn = MagicMock(return_value=FakeRunResult(output="CLI for security"))

        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=cli_fn,
            models=FREE_MODELS[:3],
        )
        result = router.run(
            "Run security audit and credential rotation on production",
            tags={"critical", "operator", "security"},
            tier="PROD_WRITE",
            working_dir="/tmp",
        )

        assert result.backend == "claude_cli"
        assert result.complexity is not None
        assert result.complexity.needs_cli
        client.chat.assert_not_called()

    def test_simple_confirm_stays_free(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = OpenRouterResponse(
            text="Confirmed",
            model=FREE_MODELS[0].id,
            tokens_input=5,
            tokens_output=10,
            finish_reason="stop",
            latency_ms=100,
        )

        cli_fn = MagicMock()
        router = LLMRouter(
            openrouter_client=client,
            claude_runner_fn=cli_fn,
            models=FREE_MODELS[:3],
        )
        result = router.run(
            "Check if the report was sent",
            tags={"confirm", "email"},
        )

        assert result.backend == "openrouter"
        cli_fn.assert_not_called()


class TestCooldownPersistence:
    """Verify cooldowns persist across multiple run() calls."""

    def test_cooled_model_skipped_on_next_call(self) -> None:
        responses = iter(
            [
                OpenRouterError(
                    status_code=429,
                    error_type="rate_limit",
                    message="RL",
                    is_rate_limit=True,
                    retry_after=300,
                ),
                OpenRouterResponse(
                    text="B first call",
                    model="b",
                    tokens_input=1,
                    tokens_output=1,
                    finish_reason="stop",
                    latency_ms=50,
                ),
                # Second call — A still cooled, B serves again
                OpenRouterResponse(
                    text="B second call",
                    model="b",
                    tokens_input=1,
                    tokens_output=1,
                    finish_reason="stop",
                    latency_ms=50,
                ),
            ]
        )

        client = MagicMock(spec=OpenRouterClient)
        client.chat.side_effect = lambda *a, **kw: next(responses)

        router = LLMRouter(
            openrouter_client=client,
            models=FREE_MODELS[:2],
            cooldown_seconds=300,
        )

        r1 = router.run("First call")
        assert r1.text == "B first call"

        r2 = router.run("Second call")
        assert r2.text == "B second call"

        # Model A should not have been tried on second call
        calls = [c[1]["model"] for c in client.chat.call_args_list]
        assert calls == [FREE_MODELS[0].id, FREE_MODELS[1].id, FREE_MODELS[1].id]


class TestResultCompat:
    """Verify LLMResult is compatible with task_runner.py expectations."""

    def test_openrouter_result_has_all_compat_fields(self) -> None:
        client = MagicMock(spec=OpenRouterClient)
        client.chat.return_value = OpenRouterResponse(
            text="ok",
            model="m",
            tokens_input=10,
            tokens_output=20,
            finish_reason="stop",
            latency_ms=100,
        )

        router = LLMRouter(openrouter_client=client, models=FREE_MODELS[:1])
        result = router.run("test")

        # All fields task_runner.py accesses
        assert hasattr(result, "output")
        assert hasattr(result, "tokens_input")
        assert hasattr(result, "tokens_output")
        assert hasattr(result, "cost_usd")
        assert hasattr(result, "num_turns")
        assert hasattr(result, "timed_out")
        assert hasattr(result, "exit_code")
        assert hasattr(result, "duration_seconds")
        assert hasattr(result, "destructive_warnings")

        # OpenRouter results should have safe defaults
        assert result.exit_code == 0
        assert not result.timed_out
        assert result.destructive_warnings == ()
        assert result.num_turns == 0
