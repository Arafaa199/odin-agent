from __future__ import annotations

from abc import ABC, abstractmethod


class Transport(ABC):
    """Abstract base for agent notification transports."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def send(self, message: str) -> bool: ...

    def ask(self, question: str, timeout_ms: int = 300000) -> str:
        """Bidirectional: send question and wait for reply.
        Default: send-only transports return UNSUPPORTED."""
        if self.send(question):
            return "UNSUPPORTED"
        return "SEND_FAILED"
