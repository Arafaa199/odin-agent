"""Home Assistant REST API client for Odin on the worker host.

Standalone CLI for controlling HA devices. Mirrors the mobile app's Home Assistant logic:
entity fuzzy matching, service calls, state queries, scene triggering.

Usage:
    python ha_control.py action <entity_query> <action> [value]
    python ha_control.py state <entity_query>
    python ha_control.py list [domain_filter]
    python ha_control.py scene <scene_query>

Auth: HA_TOKEN env var or ~/.odin/secrets/ha_token file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

log = logging.getLogger("ha_control")

HA_BASE = os.environ.get("HA_URL", "http://localhost:8123")
CONTROLLABLE_DOMAINS = {
    "light",
    "switch",
    "fan",
    "cover",
    "climate",
    "input_boolean",
    "scene",
    "script",
    "media_player",
}

_entity_cache: list[dict] = []
_cache_ts: float = 0
CACHE_TTL = 300


def _get_token() -> str:
    token = os.environ.get("HA_TOKEN", "")
    if token:
        return token
    token_file = Path.home() / ".odin" / "secrets" / "ha_token"
    if token_file.exists():
        return token_file.read_text().strip()
    return ""


def _ha_request(
    path: str, method: str = "GET", data: dict | None = None, timeout: int = 10
) -> tuple[int, any]:
    token = _get_token()
    if not token:
        return 0, {
            "error": "No HA token configured. Set HA_TOKEN env or create ~/.odin/secrets/ha_token"
        }

    url = f"{HA_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"HTTP {e.code}: {e.reason}"}
    except urllib.error.URLError as e:
        return 0, {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return 0, {"error": str(e)}


def _refresh_cache() -> list[dict]:
    global _entity_cache, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL and _entity_cache:
        return _entity_cache

    status, states = _ha_request("/api/states")
    if status != 200 or not isinstance(states, list):
        log.error(f"Failed to fetch HA states: {status}")
        return _entity_cache

    entities = []
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if domain not in CONTROLLABLE_DOMAINS:
            continue
        attrs = s.get("attributes", {})
        entities.append(
            {
                "entity_id": eid,
                "domain": domain,
                "friendly_name": attrs.get("friendly_name", eid),
                "state": s.get("state", "unknown"),
            }
        )

    _entity_cache = entities
    _cache_ts = time.time()
    return entities


def _fuzzy_score(query: str, entity: dict) -> int:
    target = entity["friendly_name"].lower()
    eid = entity["entity_id"].replace("_", " ").lower()

    score = 0
    if target == query:
        return 200
    if query in target:
        score += 100

    query_words = query.split()
    target_words = target.split()
    id_words = eid.split(".")[-1].split() if "." in eid else eid.split()

    matched = [
        w
        for w in query_words
        if any(w in tw for tw in target_words) or any(w in iw for iw in id_words)
    ]
    score += len(matched) * 30
    if len(matched) == len(query_words):
        score += 50

    return score


def resolve_entity(query: str) -> dict | None:
    entities = _refresh_cache()
    lower = query.lower()

    exact = next((e for e in entities if e["friendly_name"].lower() == lower), None)
    if exact:
        return exact

    scored = [(e, _fuzzy_score(lower, e)) for e in entities]
    scored = [(e, s) for e, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0] if scored else None


def _map_action(entity: dict, action: str, value: str | None) -> tuple[str | None, dict]:
    domain = entity["domain"]
    if action == "turn_on":
        return "turn_on", {}
    elif action == "turn_off":
        return "turn_off", {}
    elif action == "toggle":
        return "toggle", {}
    elif action == "set_brightness":
        if domain != "light" or not value:
            return None, {}
        try:
            pct = max(0, min(100, int(value)))
        except ValueError:
            return None, {}
        return "turn_on", {"brightness_pct": pct}
    elif action == "set_temperature":
        if domain != "climate" or not value:
            return None, {}
        try:
            temp = float(value)
        except ValueError:
            return None, {}
        return "set_temperature", {"temperature": temp}
    return None, {}


def call_service(entity: dict, action: str, value: str | None = None) -> dict:
    service, service_data = _map_action(entity, action, value)
    if not service:
        return {
            "success": False,
            "error": f"Unsupported action '{action}' for {entity['domain']} device",
        }

    body = {"entity_id": entity["entity_id"]}
    body.update(service_data)

    status, resp = _ha_request(
        f"/api/services/{entity['domain']}/{service}", method="POST", data=body
    )
    if 200 <= status < 300:
        name = entity["friendly_name"]
        if action == "set_brightness":
            msg = f"Set {name} brightness to {value}%"
        elif action == "set_temperature":
            msg = f"Set {name} temperature to {value}°"
        elif action == "turn_on":
            msg = f"Turned on {name}"
        elif action == "turn_off":
            msg = f"Turned off {name}"
        elif action == "toggle":
            msg = f"Toggled {name}"
        else:
            msg = f"Done: {action} on {name}"
        return {"success": True, "message": msg}
    return {"success": False, "error": f"HA returned status {status}: {resp}"}


def get_state(query: str) -> dict:
    token = _get_token()
    if not token:
        return {"success": False, "error": "No HA token configured"}

    status, states = _ha_request("/api/states")
    if status != 200 or not isinstance(states, list):
        return {"success": False, "error": f"Failed to fetch states: {status}"}

    lower = query.lower()
    matches = []
    for s in states:
        eid = s.get("entity_id", "")
        name = s.get("attributes", {}).get("friendly_name", eid).lower()
        if lower in name or lower in eid.replace("_", " "):
            matches.append(s)

    if not matches:
        return {"success": False, "error": f"No entity matching '{query}'"}

    results = []
    for s in matches[:5]:
        attrs = s.get("attributes", {})
        info = {
            "entity_id": s["entity_id"],
            "friendly_name": attrs.get("friendly_name", s["entity_id"]),
            "state": s.get("state", "unknown"),
        }
        if "current_temperature" in attrs:
            info["current_temperature"] = attrs["current_temperature"]
        if "temperature" in attrs:
            info["target_temperature"] = attrs["temperature"]
        if "brightness" in attrs:
            info["brightness"] = attrs["brightness"]
        if "unit_of_measurement" in attrs:
            info["unit"] = attrs["unit_of_measurement"]
        results.append(info)

    return {"success": True, "states": results}


def trigger_scene(query: str) -> dict:
    entity = resolve_entity(query)
    if not entity:
        entities = _refresh_cache()
        scenes = [e for e in entities if e["domain"] == "scene"]
        lower = query.lower()
        match = next((s for s in scenes if lower in s["friendly_name"].lower()), None)
        if not match:
            return {"success": False, "error": f"No scene matching '{query}'"}
        entity = match

    if entity["domain"] != "scene":
        return {"success": False, "error": f"'{entity['friendly_name']}' is not a scene"}

    status, resp = _ha_request(
        "/api/services/scene/turn_on",
        method="POST",
        data={"entity_id": entity["entity_id"]},
    )
    if 200 <= status < 300:
        return {"success": True, "message": f"Activated scene: {entity['friendly_name']}"}
    return {"success": False, "error": f"Failed to activate scene: {status}"}


def list_entities(domain_filter: str | None = None) -> dict:
    entities = _refresh_cache()
    if domain_filter:
        entities = [e for e in entities if e["domain"] == domain_filter]
    summary = {}
    for e in entities:
        d = e["domain"]
        if d not in summary:
            summary[d] = []
        summary[d].append(f"{e['friendly_name']} ({e['state']})")
    return {"success": True, "entities": summary, "total": len(entities)}


def main():
    if len(sys.argv) < 2:
        print("Usage: ha_control.py <command> [args...]")
        print("Commands: action, state, list, scene")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "action":
        if len(sys.argv) < 4:
            print(
                json.dumps({"success": False, "error": "Usage: action <entity> <action> [value]"})
            )
            sys.exit(1)
        query = sys.argv[2]
        action = sys.argv[3]
        value = sys.argv[4] if len(sys.argv) > 4 else None

        entity = resolve_entity(query)
        if not entity:
            print(json.dumps({"success": False, "error": f"No device matching '{query}'"}))
            sys.exit(1)

        result = call_service(entity, action, value)
        print(json.dumps(result))

    elif cmd == "state":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: state <entity_query>"}))
            sys.exit(1)
        result = get_state(sys.argv[2])
        print(json.dumps(result))

    elif cmd == "list":
        domain = sys.argv[2] if len(sys.argv) > 2 else None
        result = list_entities(domain)
        print(json.dumps(result, indent=2))

    elif cmd == "scene":
        if len(sys.argv) < 3:
            print(json.dumps({"success": False, "error": "Usage: scene <scene_name>"}))
            sys.exit(1)
        result = trigger_scene(sys.argv[2])
        print(json.dumps(result))

    else:
        print(json.dumps({"success": False, "error": f"Unknown command: {cmd}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
