"""Lazy singleton for LifeOS unified memory client."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("infra.memory")

_client = None
_initialized = False


def get_memory_client():
    """Return LifeOSMemory instance or None if no key available."""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    key = os.environ.get("INTAKE_KEY", "")
    if not key:
        log.debug("No INTAKE_KEY — memory client disabled")
        return None

    try:
        from ..lifeos_memory import LifeOSMemory

        _client = LifeOSMemory(intake_key=key, agent="task_runner")
        log.info("LifeOS memory client initialized")
    except Exception as e:
        log.warning("Failed to init memory client: %s", e)
        _client = None

    return _client


_plaud_client = None
_plaud_initialized = False


def get_plaud_memory_client():
    """Return LifeOSMemory instance for plaud ingest (agent='plaud')."""
    global _plaud_client, _plaud_initialized
    if _plaud_initialized:
        return _plaud_client
    _plaud_initialized = True

    key = os.environ.get("INTAKE_KEY", "")
    if not key:
        log.debug("No INTAKE_KEY — plaud memory client disabled")
        return None

    try:
        from ..lifeos_memory import LifeOSMemory

        _plaud_client = LifeOSMemory(intake_key=key, agent="plaud")
        log.info("Plaud memory client initialized")
    except Exception as e:
        log.warning("Failed to init plaud memory client: %s", e)
        _plaud_client = None

    return _plaud_client
