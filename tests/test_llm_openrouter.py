"""Tests for src.llm.openrouter_client — OpenRouter HTTP client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from src.llm.openrouter_client import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterResponse,
)


def _mock_response(
    status_code: int = 200, json_data: dict | None = None, text: str = ""
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


class TestOpenRouterClientSuccess:
    def test_successful_chat(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(
            200,
            {
                "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "test/model",
            },
        )

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterResponse)
        assert result.text == "Hello!"
        assert result.tokens_input == 10
        assert result.tokens_output == 5
        assert result.model == "test/model"
        assert result.finish_reason == "stop"

    def test_json_mode(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(
            200,
            {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {},
                "model": "test/model",
            },
        )

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp) as mock_post:
            client.chat("test/model", [{"role": "user", "content": "json"}], json_mode=True)
            payload = mock_post.call_args[1]["json"]
            assert payload["response_format"] == {"type": "json_object"}


class TestOpenRouterClientErrors:
    def test_rate_limit_429(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(429, text="Rate limited")
        mock_resp.headers = {"Retry-After": "45"}

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.is_rate_limit
        assert result.retry_after == 45.0
        assert result.status_code == 429

    def test_auth_error_401(self) -> None:
        client = OpenRouterClient(api_key="bad-key")
        mock_resp = _mock_response(401, text="Unauthorized")

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.is_auth_error
        assert result.status_code == 401

    def test_server_error_500(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(
            500,
            {
                "error": {"type": "server_error", "message": "Internal error"},
            },
        )

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.status_code == 500
        assert "Internal error" in result.message

    def test_timeout(self) -> None:
        import requests as req

        client = OpenRouterClient(api_key="test-key", timeout=1)
        with patch("src.llm.openrouter_client.requests.post", side_effect=req.Timeout):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.status_code == 408
        assert result.error_type == "timeout"

    def test_connection_error(self) -> None:
        import requests as req

        client = OpenRouterClient(api_key="test-key")
        with patch(
            "src.llm.openrouter_client.requests.post", side_effect=req.ConnectionError("refused")
        ):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.status_code == 0
        assert result.error_type == "connection"

    def test_empty_choices(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(200, {"choices": [], "usage": {}})

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.error_type == "empty_response"

    def test_rate_limit_in_error_body(self) -> None:
        client = OpenRouterClient(api_key="test-key")
        mock_resp = _mock_response(
            402,
            {
                "error": {"type": "billing", "message": "Rate limit exceeded for free tier"},
            },
        )

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp):
            result = client.chat("test/model", [{"role": "user", "content": "Hi"}])

        assert isinstance(result, OpenRouterError)
        assert result.is_rate_limit


class TestOpenRouterClientHeaders:
    def test_correct_headers_sent(self) -> None:
        client = OpenRouterClient(api_key="my-secret-key")
        mock_resp = _mock_response(
            200,
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
                "model": "m",
            },
        )

        with patch("src.llm.openrouter_client.requests.post", return_value=mock_resp) as mock_post:
            client.chat("test/model", [{"role": "user", "content": "Hi"}])
            headers = mock_post.call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer my-secret-key"
            assert headers["X-Title"] == "Odin Agent"
