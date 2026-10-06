from __future__ import annotations

import json
import logging
from urllib.request import Request, urlopen

from .base import Transport

log = logging.getLogger("imessage")


class IMessageTransport(Transport):
    """Send-only transport via an iMessage bridge (imsg CLI on a Mac, port 3342)."""

    def __init__(
        self, url: str = "http://localhost:3342", recipient: str = "", bridge_key: str = ""
    ):
        self._url = url
        self._recipient = recipient
        self._key = bridge_key

    @property
    def name(self) -> str:
        return "imessage"

    def available(self) -> bool:
        if not self._key or not self._recipient:
            return False
        try:
            req = Request(f"{self._url}/health", method="GET")
            with urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def send(self, message: str) -> bool:
        if not self._recipient or not self._key:
            log.debug("iMessage send skipped: no recipient or bridge key configured")
            return False
        try:
            payload = json.dumps(
                {"to": self._recipient, "text": message, "service": "auto"}
            ).encode()
            req = Request(
                f"{self._url}/messages/send",
                data=payload,
                headers={"Content-Type": "application/json", "X-Bridge-Key": self._key},
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                return resp.status == 200
        except Exception as e:
            log.debug(f"iMessage send failed: {e}")
            return False
