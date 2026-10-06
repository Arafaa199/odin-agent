"""LLM Router — free OpenRouter models with Claude CLI fallback.

Routing logic:
  1. Score task complexity
  2. If complex → Claude CLI directly
  3. Otherwise → try free OpenRouter models in order
  4. On rate limit → rotate to next model, cooldown the current one
  5. If ALL free models exhausted → Claude CLI as last resort
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

from .complexity import ComplexityScore, score_complexity
from .models import FREE_MODELS, FreeModel
from .openrouter_client import OpenRouterClient, OpenRouterError

log = logging.getLogger("llm.router")

DEFAULT_COOLDOWN_SECONDS = 60
MAX_RETRIES_PER_REQUEST = 3


@dataclass(frozen=True)
class LLMResult:
    """Unified result from any LLM backend.

    Fields mirror claude_runner.RunResult where applicable, so task_runner.py
    can consume this without large refactors.
    """

    text: str
    model: str
    backend: str  # "openrouter" or "claude_cli"
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1
    complexity: ComplexityScore | None = None
    # Compat fields for task_runner.py (RunResult parity)
    exit_code: int = 0
    timed_out: bool = False
    duration_seconds: float = 0.0
    destructive_warnings: tuple[str, ...] = ()
    num_turns: int = 0

    @property
    def output(self) -> str:
        """Alias for text — matches RunResult.output."""
        return self.text


@dataclass
class ModelCooldown:
    """Tracks per-model cooldown state."""

    model_id: str
    until: float = 0.0  # monotonic timestamp
    consecutive_failures: int = 0

    @property
    def is_cooled_down(self) -> bool:
        return time.monotonic() < self.until

    def apply_cooldown(self, seconds: float) -> None:
        self.until = time.monotonic() + seconds
        self.consecutive_failures += 1

    def reset(self) -> None:
        self.until = 0.0
        self.consecutive_failures = 0


class LLMRouter:
    """Routes LLM requests through free models with Claude CLI fallback."""

    def __init__(
        self,
        openrouter_client: OpenRouterClient | None = None,
        claude_runner_fn: object | None = None,
        models: Sequence[FreeModel] | None = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._client = openrouter_client or OpenRouterClient()
        self._claude_fn = claude_runner_fn
        self._models = tuple(models) if models is not None else FREE_MODELS
        self._cooldown_seconds = cooldown_seconds
        self._cooldowns: dict[str, ModelCooldown] = {}

    def _get_cooldown(self, model_id: str) -> ModelCooldown:
        if model_id not in self._cooldowns:
            self._cooldowns[model_id] = ModelCooldown(model_id=model_id)
        return self._cooldowns[model_id]

    def _available_models(self) -> list[FreeModel]:
        """Return models not currently in cooldown."""
        return [m for m in self._models if not self._get_cooldown(m.id).is_cooled_down]

    def get_status(self) -> dict:
        """Return current router state for diagnostics."""
        now = time.monotonic()
        model_status = []
        for m in self._models:
            cd = self._get_cooldown(m.id)
            remaining = max(0, cd.until - now)
            model_status.append(
                {
                    "model": m.id,
                    "name": m.name,
                    "available": not cd.is_cooled_down,
                    "cooldown_remaining_s": round(remaining, 1),
                    "consecutive_failures": cd.consecutive_failures,
                }
            )
        available = self._available_models()
        return {
            "total_models": len(self._models),
            "available_models": len(available),
            "all_exhausted": len(available) == 0,
            "models": model_status,
        }

    def run(
        self,
        prompt: str,
        *,
        tags: frozenset[str] | set[str] | None = None,
        tier: str = "AUTO",
        working_dir: str = ".",
        timeout_minutes: int = 15,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        force_cli: bool = False,
    ) -> LLMResult:
        """Route a prompt through the best available backend.

        Args:
            prompt: The user/task prompt.
            tags: Task tags for complexity scoring.
            tier: Safety tier (AUTO, PROD_READONLY, PROD_WRITE).
            working_dir: For Claude CLI execution context.
            timeout_minutes: Max time for Claude CLI.
            system_prompt: Optional system message.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
            force_cli: Skip free models, go straight to Claude CLI.
        """
        complexity = score_complexity(prompt, tags, tier)
        log.info("Complexity: %s", complexity.reason)

        # Route to Claude CLI for complex tasks or if forced
        if (complexity.needs_cli or force_cli) and self._claude_fn is not None:
            log.info(
                "Routing to Claude CLI (complex=%s, forced=%s)", complexity.needs_cli, force_cli
            )
            return self._run_claude_cli(prompt, working_dir, timeout_minutes, complexity)

        # Try free OpenRouter models
        result = self._try_openrouter(prompt, system_prompt, temperature, max_tokens, complexity)
        if result is not None:
            return result

        # All free models exhausted — fall back to Claude CLI
        if self._claude_fn is not None:
            log.warning("All free models exhausted — falling back to Claude CLI")
            return self._run_claude_cli(prompt, working_dir, timeout_minutes, complexity)

        # No Claude CLI available either — return error
        log.error("All backends exhausted — no Claude CLI configured")
        return LLMResult(
            text="ERROR: All free models rate-limited and no Claude CLI fallback configured.",
            model="none",
            backend="error",
            complexity=complexity,
        )

    def _try_openrouter(
        self,
        prompt: str,
        system_prompt: str | None,
        temperature: float,
        max_tokens: int,
        complexity: ComplexityScore,
    ) -> LLMResult | None:
        """Try each available free model. Returns None if all exhausted."""
        available = self._available_models()
        if not available:
            log.warning("No free models available (all in cooldown)")
            return None

        attempts = 0
        for model in available:
            if attempts >= MAX_RETRIES_PER_REQUEST:
                break

            attempts += 1
            log.info("Trying %s (%s)", model.name, model.id)

            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            result = self._client.chat(
                model=model.id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            if isinstance(result, OpenRouterError):
                self._handle_error(model, result)
                continue

            # Success — reset cooldown and return
            self._get_cooldown(model.id).reset()
            log.info(
                "Success via %s (%dms, %d tokens out)",
                model.name,
                result.latency_ms,
                result.tokens_output,
            )

            return LLMResult(
                text=result.text,
                model=result.model,
                backend="openrouter",
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                cost_usd=0.0,
                latency_ms=result.latency_ms,
                attempts=attempts,
                complexity=complexity,
                duration_seconds=result.latency_ms / 1000.0,
            )

        return None

    def _handle_error(self, model: FreeModel, error: OpenRouterError) -> None:
        """Apply cooldown and log the error."""
        cd = self._get_cooldown(model.id)

        if error.is_rate_limit:
            cooldown = max(self._cooldown_seconds, error.retry_after)
            # Exponential backoff on consecutive failures
            cooldown *= min(4.0, 1.5**cd.consecutive_failures)
            cd.apply_cooldown(cooldown)
            log.warning(
                "Rate limited on %s — cooldown %.0fs (failures: %d)",
                model.name,
                cooldown,
                cd.consecutive_failures,
            )
        elif error.is_auth_error:
            # Auth errors get long cooldown — key probably bad
            cd.apply_cooldown(3600)
            log.error("Auth error on %s — cooldown 1h: %s", model.name, error.message)
        else:
            cd.apply_cooldown(self._cooldown_seconds)
            log.warning("Error on %s [%d]: %s", model.name, error.status_code, error.message)

    def _run_claude_cli(
        self,
        prompt: str,
        working_dir: str,
        timeout_minutes: int,
        complexity: ComplexityScore,
    ) -> LLMResult:
        """Execute via Claude CLI subprocess."""
        if self._claude_fn is None:
            return LLMResult(
                text="ERROR: Claude CLI not configured",
                model="none",
                backend="error",
                complexity=complexity,
            )

        start = time.monotonic()
        try:
            result = self._claude_fn(prompt, working_dir, timeout_minutes=timeout_minutes)
            latency = int((time.monotonic() - start) * 1000)
            duration = time.monotonic() - start

            return LLMResult(
                text=result.output,
                model="claude-cli",
                backend="claude_cli",
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                cost_usd=result.cost_usd,
                latency_ms=latency,
                attempts=1,
                complexity=complexity,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=duration,
                destructive_warnings=tuple(result.destructive_warnings),
                num_turns=result.num_turns,
            )
        except Exception as e:
            duration = time.monotonic() - start
            latency = int(duration * 1000)
            log.error("Claude CLI failed: %s", e)
            return LLMResult(
                text=f"ERROR: Claude CLI failed: {e}",
                model="claude-cli",
                backend="error",
                latency_ms=latency,
                duration_seconds=duration,
                exit_code=1,
                complexity=complexity,
            )
