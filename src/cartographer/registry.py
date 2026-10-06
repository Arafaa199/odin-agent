"""Cartographer Registry — builds registry.json from scans + known_services.yaml.

Merges discovered services with the curated known_services.yaml baseline.
Tags each service as documented/undocumented.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..config import config_path
from .scanner import ScanResult
from .suppress import filter_services

REGISTRY_DIR = Path.home() / ".odin" / ".cartographer"
REGISTRY_PATH = REGISTRY_DIR / "registry.json"


def load_known_services(path: Optional[Path] = None) -> dict[str, dict]:
    """Load config/known_services.yaml (or the example). Missing file = empty baseline."""
    path = path or config_path("known_services", "ODIN_KNOWN_SERVICES")
    if not path.exists():
        return {}

    with open(path) as f:
        data = yaml.safe_load(f) or []

    known = {}
    for entry in data if isinstance(data, list) else []:
        key = _service_key(entry["name"], entry["host"], entry.get("type", ""))
        known[key] = entry
    return known


def _build_port_index(known_services: dict[str, dict]) -> dict[str, dict]:
    """Build (host, port) → known_service lookup for port-type services only."""
    index = {}
    for entry in known_services.values():
        if entry.get("port") and entry.get("type") == "port":
            key = f"{entry['host']}:{entry['port']}"
            index[key] = entry
    return index


def _build_all_port_index(known_services: dict[str, dict]) -> set[tuple[str, int]]:
    """Build set of (host, port) for ALL known services (any type) that have a port."""
    return {
        (entry["host"], entry["port"]) for entry in known_services.values() if entry.get("port")
    }


def _service_key(name: str, host: str, stype: str) -> str:
    return f"{host}:{stype}:{name}"


def _normalize_state(svc: dict) -> str:
    state = svc.get("state", "unknown")
    sub = svc.get("sub_state", "")
    if state == "active" and sub == "running":
        return "running"
    if state == "active" and sub in ("waiting", "start"):
        return "waiting"
    if state == "activating":
        return "starting"
    if state == "failed":
        return "failed"
    return state


def _dedup_ports(services: list[dict]) -> list[dict]:
    """Deduplicate port entries by (host, port), keeping the one with process info."""
    port_best = {}
    non_ports = []
    for svc in services:
        if svc.get("type") != "port":
            non_ports.append(svc)
            continue
        key = (svc["host"], svc.get("port", 0))
        existing = port_best.get(key)
        if existing is None:
            port_best[key] = svc
        elif svc.get("process") and not existing.get("process"):
            port_best[key] = svc
    return non_ports + list(port_best.values())


def _absorb_redundant_ports(services: list[dict]) -> list[dict]:
    """Remove port entries that duplicate an already-matched non-port service on the same (host, port)."""
    claimed_ports = set()
    for svc in services:
        if svc["type"] != "port" and svc.get("port"):
            claimed_ports.add((svc["host"], svc["port"]))

    result = []
    for svc in services:
        if svc["type"] == "port" and svc.get("port"):
            if (svc["host"], svc["port"]) in claimed_ports:
                continue
        result.append(svc)
    return result


def build_registry(
    scan_results: list[ScanResult],
    known_services: Optional[dict[str, dict]] = None,
) -> dict:
    if known_services is None:
        known_services = load_known_services()

    port_index = _build_port_index(known_services)
    all_port_index = _build_all_port_index(known_services)
    matched_known_keys = set()
    all_services = []
    scan_errors = {}

    for scan in scan_results:
        if scan.errors:
            scan_errors[scan.host] = scan.errors

        filtered = filter_services(scan.services)
        filtered = _dedup_ports(filtered)

        for svc in filtered:
            name = svc.get("name", "")
            if not name:
                continue

            key = _service_key(name, svc["host"], svc["type"])
            known = known_services.get(key)

            if not known and svc["type"] == "port" and svc.get("port"):
                port_key = f"{svc['host']}:{svc['port']}"
                known = port_index.get(port_key)
                if not known and (svc["host"], svc["port"]) in all_port_index:
                    continue

            if known:
                known_key = _service_key(known["name"], known["host"], known.get("type", ""))
                matched_known_keys.add(known_key)

            entry = {
                "name": known["name"] if known else name,
                "host": svc["host"],
                "type": svc["type"],
                "state": _normalize_state(svc),
                "documented": known is not None,
            }

            if known:
                entry["category"] = known.get("category", "unknown")
                entry["description"] = known.get("description", "")
                expected = known.get("expected_state", "running")
                if expected != "any":
                    entry["expected_state"] = expected
                    entry["healthy"] = _is_healthy(entry["state"], expected)
                else:
                    entry["healthy"] = True
                if known.get("port"):
                    entry["port"] = known["port"]
            else:
                entry["category"] = "undocumented"
                entry["description"] = svc.get("description", "")
                entry["healthy"] = None

            for extra in ("port", "image", "process", "bind", "path", "schedule", "pid", "status"):
                if extra in svc and extra not in entry:
                    entry[extra] = svc[extra]

            all_services.append(entry)

    for key, known in known_services.items():
        if key not in matched_known_keys:
            expected = known.get("expected_state", "running")
            all_services.append(
                {
                    "name": known["name"],
                    "host": known["host"],
                    "type": known.get("type", "unknown"),
                    "state": "not_found",
                    "documented": True,
                    "category": known.get("category", "unknown"),
                    "description": known.get("description", ""),
                    "expected_state": expected,
                    "healthy": False if expected != "any" else None,
                }
            )

    all_services.sort(key=lambda s: (s["host"], s["type"], s["name"]))

    now = datetime.now(timezone.utc).isoformat()
    scan_times = {s.host: s.scan_time for s in scan_results}

    return {
        "generated_at": now,
        "scan_times": scan_times,
        "scan_errors": scan_errors,
        "summary": _build_summary(all_services),
        "services": all_services,
    }


def _is_healthy(actual_state: str, expected_state: str) -> bool:
    if expected_state == "running":
        return actual_state in ("running", "waiting", "listening", "present", "scheduled")
    if expected_state == "stopped":
        return actual_state in ("stopped", "not_found", "unloaded")
    return True


def _build_summary(services: list[dict]) -> dict:
    total = len(services)
    documented = sum(1 for s in services if s["documented"])
    undocumented = total - documented
    healthy = sum(1 for s in services if s.get("healthy") is True)
    unhealthy = sum(1 for s in services if s.get("healthy") is False)
    not_found = sum(1 for s in services if s["state"] == "not_found")

    by_host = {}
    for svc in services:
        host = svc["host"]
        if host not in by_host:
            by_host[host] = {"total": 0, "healthy": 0, "unhealthy": 0, "undocumented": 0}
        by_host[host]["total"] += 1
        if svc.get("healthy") is True:
            by_host[host]["healthy"] += 1
        elif svc.get("healthy") is False:
            by_host[host]["unhealthy"] += 1
        if not svc["documented"]:
            by_host[host]["undocumented"] += 1

    by_category = {}
    for svc in services:
        cat = svc.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1

    return {
        "total": total,
        "documented": documented,
        "undocumented": undocumented,
        "healthy": healthy,
        "unhealthy": unhealthy,
        "not_found": not_found,
        "by_host": by_host,
        "by_category": by_category,
    }


def save_registry(registry: dict, path: Optional[Path] = None) -> Path:
    path = path or REGISTRY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
    return path
