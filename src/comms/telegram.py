from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen

from .base import Transport

log = logging.getLogger("telegram")

DEFAULT_URL = "http://localhost:3340"


def available(wa_url: str = DEFAULT_URL) -> bool:
    try:
        req = Request(f"{wa_url}/status", method="GET")
        with urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get("connection") == "open"
    except Exception:
        return False


def send(message: str, wa_url: str = DEFAULT_URL) -> bool:
    try:
        payload = json.dumps({"message": message}).encode()
        req = Request(
            f"{wa_url}/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")
        return False


def ask(question: str, timeout_ms: int = 300000, wa_url: str = DEFAULT_URL) -> str:
    http_timeout = timeout_ms // 1000 + 30
    try:
        payload = json.dumps({"question": question, "timeout": timeout_ms}).encode()
        req = Request(
            f"{wa_url}/ask",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=http_timeout) as resp:
            data = json.loads(resp.read())
            if data.get("success"):
                return data.get("reply", "")
            return "TIMEOUT"
    except Exception as e:
        log.debug(f"Telegram ask failed: {e}")
        return "BRIDGE_UNREACHABLE"


class TelegramTransport(Transport):
    def __init__(self, url: str = DEFAULT_URL):
        self._url = url

    @property
    def name(self) -> str:
        return "telegram"

    def available(self) -> bool:
        return available(self._url)

    def send(self, message: str) -> bool:
        return send(message, self._url)

    def ask(self, question: str, timeout_ms: int = 300000) -> str:
        return ask(question, timeout_ms, self._url)
