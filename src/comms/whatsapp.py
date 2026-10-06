from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen

from .base import Transport

log = logging.getLogger("whatsapp")

DEFAULT_URL = "http://localhost:3345"


class WhatsAppTransport(Transport):
    def __init__(self, url: str = DEFAULT_URL):
        self._url = url

    @property
    def name(self) -> str:
        return "whatsapp"

    def available(self) -> bool:
        try:
            req = Request(f"{self._url}/status", method="GET")
            with urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
                return data.get("connection") == "open"
        except Exception:
            return False

    def send(self, message: str) -> bool:
        try:
            payload = json.dumps({"message": message}).encode()
            req = Request(
                f"{self._url}/send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            log.debug(f"WhatsApp send failed: {e}")
            return False

    def ask(self, question: str, timeout_ms: int = 300000) -> str:
        http_timeout = timeout_ms // 1000 + 30
        try:
            payload = json.dumps({"question": question, "timeout": timeout_ms}).encode()
            req = Request(
                f"{self._url}/ask",
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
            log.debug(f"WhatsApp ask failed: {e}")
            return "BRIDGE_UNREACHABLE"
