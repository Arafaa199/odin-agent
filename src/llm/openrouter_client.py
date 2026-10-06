"""OpenRouter HTTP client — OpenAI-compatible chat completions."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("llm.openrouter")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class OpenRouterResponse:
    text: str
    model: str
    tokens_input: int = 0
    tokens_output: int = 0
    finish_reason: str = ""
    latency_ms: int = 0


@dataclass(frozen=True)
class OpenRouterError:
    status_code: int
    error_type: str
    message: str
    is_rate_limit: bool = False
    is_auth_error: bool = False
    retry_after: float = 0.0


class OpenRouterClient:
    """Stateless client for OpenRouter chat completions."""

    def __init__(self, api_key: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._timeout = timeout
        if not self._api_key:
            log.warning("No OPENROUTER_API_KEY — calls will fail")

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> OpenRouterResponse | OpenRouterError:
        """Send a chat completion request. Returns response or typed error."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://localhost"),
            "X-Title": "Odin Agent",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        start = time.monotonic()
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        except requests.Timeout:
            return OpenRouterError(
                status_code=408,
                error_type="timeout",
                message=f"Request timed out after {self._timeout}s",
            )
        except requests.ConnectionError as e:
            return OpenRouterError(
                status_code=0,
                error_type="connection",
                message=str(e),
            )

        latency = int((time.monotonic() - start) * 1000)

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "30"))
            return OpenRouterError(
                status_code=429,
                error_type="rate_limit",
                message="Rate limited",
                is_rate_limit=True,
                retry_after=retry_after,
            )

        if resp.status_code == 401 or resp.status_code == 403:
            return OpenRouterError(
                status_code=resp.status_code,
                error_type="auth",
                message=resp.text[:200],
                is_auth_error=True,
            )

        if resp.status_code >= 400:
            body = _safe_json(resp)
            err_msg = (
                body.get("error", {}).get("message", resp.text[:200]) if body else resp.text[:200]
            )
            is_rl = "rate" in err_msg.lower() or "limit" in err_msg.lower()
            return OpenRouterError(
                status_code=resp.status_code,
                error_type=body.get("error", {}).get("type", "unknown") if body else "unknown",
                message=err_msg,
                is_rate_limit=is_rl,
                retry_after=30.0 if is_rl else 0.0,
            )

        body = resp.json()
        choices = body.get("choices", [])
        if not choices:
            return OpenRouterError(
                status_code=resp.status_code,
                error_type="empty_response",
                message="No choices in response",
            )

        choice = choices[0]
        usage = body.get("usage", {})

        return OpenRouterResponse(
            text=choice.get("message", {}).get("content", ""),
            model=body.get("model", model),
            tokens_input=usage.get("prompt_tokens", 0),
            tokens_output=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", ""),
            latency_ms=latency,
        )


def _safe_json(resp: requests.Response) -> dict | None:
    try:
        return resp.json()
    except (ValueError, KeyError):
        return None
