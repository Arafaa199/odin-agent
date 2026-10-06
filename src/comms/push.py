from __future__ import annotations

import json
import logging
import uuid
from urllib.request import Request, urlopen

from .base import Transport

log = logging.getLogger("push")


class PushTransport(Transport):
    def __init__(self, intake_url: str = "http://localhost:8100", intake_key: str = ""):
        self._url = intake_url
        self._key = intake_key

    @property
    def name(self) -> str:
        return "push"

    def available(self) -> bool:
        return bool(self._key)

    def send(self, message: str) -> bool:
        if not self._key:
            return False
        try:
            payload = json.dumps(
                {
                    "event_type": "agent.alert",
                    "priority": "P1",
                    "retention_class": "short",
                    "idempotency_key": uuid.uuid4().hex,
                    "payload": {"message": message, "source": "odin"},
                }
            ).encode()
            req = Request(
                f"{self._url}/v1/events",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Intake-Key": self._key,
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            log.debug(f"Push send failed: {e}")
            return False
