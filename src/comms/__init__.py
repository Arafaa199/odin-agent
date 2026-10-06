from __future__ import annotations

import logging
import os

from .base import Transport
from . import mute
from .imessage import IMessageTransport
from .push import PushTransport
from .telegram import TelegramTransport
from .whatsapp import WhatsAppTransport

log = logging.getLogger("comms")

_registry: list[Transport] = []


def init_transports(config: dict) -> None:
    """Initialize transports from top-level config (settings.yaml root)."""
    _registry.clear()
    for t_cfg in config.get("transports", []):
        name = t_cfg.get("name")
        if not t_cfg.get("enabled", True):
            continue
        try:
            if name == "telegram":
                _registry.append(
                    TelegramTransport(
                        url=t_cfg.get("url", "http://localhost:3340"),
                    )
                )
            elif name == "imessage":
                bridge_key = t_cfg.get("bridge_key", "") or os.environ.get(
                    "IMESSAGE_BRIDGE_KEY", ""
                )
                _registry.append(
                    IMessageTransport(
                        url=t_cfg.get("url", "http://localhost:3342"),
                        recipient=t_cfg.get("recipient", ""),
                        bridge_key=bridge_key,
                    )
                )
            elif name == "whatsapp":
                _registry.append(
                    WhatsAppTransport(
                        url=t_cfg.get("url", "http://localhost:3345"),
                    )
                )
            elif name == "push":
                key = t_cfg.get("intake_key", "") or os.environ.get("INTAKE_KEY", "")
                _registry.append(
                    PushTransport(
                        intake_url=t_cfg.get(
                            "intake_url", os.environ.get("INTAKE_URL", "http://localhost:8100")
                        ),
                        intake_key=key,
                    )
                )
        except Exception as e:
            log.warning("Failed to init transport %s: %s", name, e)

    names = [t.name for t in _registry]
    log.info("Transports initialized: %s", ", ".join(names) if names else "(none)")


def get_transport() -> Transport | None:
    """Return first available transport."""
    for t in _registry:
        try:
            if t.available():
                return t
        except Exception:
            continue
    return None


def send(message: str, deadman: bool = False) -> bool:
    """Send via first available transport.

    deadman=True marks an absence-of-success page (watchdog etc.) that must
    NEVER be silenced by the incident mute switch (comms.mute).
    """
    if mute.is_muted(deadman=deadman):
        log.warning("[MUTED] send suppressed: %s", message.splitlines()[0][:80])
        return True  # muted-by-intent is not a transport failure
    t = get_transport()
    return t.send(message) if t else False


def send_all(message: str, deadman: bool = False) -> dict[str, bool]:
    """Send via preferred transport (Telegram first by registry order).

    Named `send_all` historically but deliberately single-channel: alerts go
    through Telegram only. Use individual transports directly (e.g. from
    `_registry`) for ad-hoc fan-out to WhatsApp / iMessage / Push.

    Returns {name: success} with a single entry, or {} if no transport available.
    """
    if mute.is_muted(deadman=deadman):
        log.warning("[MUTED] send_all suppressed: %s", message.splitlines()[0][:80])
        return {"muted": True}
    t = get_transport()
    if t:
        try:
            return {t.name: t.send(message)}
        except Exception as e:
            log.warning("send_all: %s failed: %s", t.name, e)
            return {t.name: False}
    return {}


def ask(question: str, timeout_ms: int = 300000) -> str:
    """Ask via first transport that supports bidirectional (Telegram)."""
    for t in _registry:
        try:
            if t.available():
                result = t.ask(question, timeout_ms)
                if result != "UNSUPPORTED":
                    return result
        except Exception:
            continue
    return "NO_TRANSPORT"


def available() -> bool:
    """True if any transport is available."""
    return get_transport() is not None
