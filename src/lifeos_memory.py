"""LifeOS Unified Agent Memory — Python client.

Uses the Intake API memory endpoints (INTAKE_URL, default localhost:8100).
All calls are fail-safe: exceptions are logged, never raised to caller.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger("lifeos_memory")

_DEFAULT_URL = "http://localhost:8100"


class LifeOSMemory:
    def __init__(
        self,
        intake_url: str | None = None,
        intake_key: str | None = None,
        agent: str = "",
        timeout: int = 10,
    ):
        self.url = (intake_url or os.environ.get("INTAKE_URL", _DEFAULT_URL)).rstrip("/")
        self.key = intake_key or os.environ.get("INTAKE_KEY", "")
        self.agent = agent
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "X-Intake-Key": self.key,
            }
        )

    def save(
        self,
        content: str,
        category: str = "state",
        tags: list[str] | None = None,
        visibility: str = "shared",
        namespace: str = "global",
        confidence: float = 0.8,
        source: str = "agent_observation",
    ) -> str | None:
        try:
            body: dict[str, Any] = {
                "content": content,
                "category": category,
                "tags": tags or [],
                "visibility": visibility,
                "namespace": namespace,
                "confidence": confidence,
                "source": source,
            }
            if self.agent:
                body["created_by"] = self.agent
            resp = self._session.post(
                f"{self.url}/v1/memory/save",
                json=body,
                timeout=self.timeout,
            )
            if resp.status_code in (200, 201):
                return resp.json().get("entry_id")
            log.warning("Memory save failed: %d %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("Memory save error: %s", e)
        return None

    def search(
        self,
        query: str,
        limit: int = 5,
        category: str | None = None,
        agent: str | None = None,
    ) -> list[dict]:
        try:
            body: dict[str, Any] = {"query": query, "limit": limit}
            if category:
                body["category"] = category
            if agent:
                body["agent"] = agent
            resp = self._session.post(
                f"{self.url}/v1/memory/search",
                json=body,
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("results", [])
            log.warning("Memory search failed: %d", resp.status_code)
        except Exception as e:
            log.warning("Memory search error: %s", e)
        return []

    def profile(self, agent: str | None = None) -> list[dict]:
        try:
            path_agent = agent or self.agent or "global"
            resp = self._session.get(
                f"{self.url}/v1/memory/profile/{path_agent}",
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json().get("entries", [])
        except Exception as e:
            log.warning("Memory profile error: %s", e)
        return []

    def remember(self, content: str, **kwargs) -> str | None:
        return self.save(content, **kwargs)

    def recall(self, query: str, limit: int = 5) -> str:
        results = self.search(query, limit=limit)
        if not results:
            return ""
        lines = []
        for r in results:
            cat = r.get("category", "?")
            text = (r.get("content") or "")[:300]
            lines.append(f"[{cat}] {text}")
        return "\n---\n".join(lines)

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass
