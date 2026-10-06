"""Noise suppression for Cartographer scanner.

Filters out system/desktop services that are not managed by this estate.
"""

from typing import Sequence

SUPPRESS_SYSTEMD_PREFIXES = (
    "at-spi-",
    "dbus.",
    "dconf.",
    "gnome-",
    "gvfs-",
    "mpris-",
    "obex.",
    "pipewire",
    "pulseaudio",
    "rpi-connect",
    "run-u",
    "tracker-",
    "wireplumber",
    "xdg-",
    "xfce4-",
)

SUPPRESS_SYSTEMD_EXACT = frozenset(
    {
        "dbus.service",
        "dconf.service",
    }
)

SUPPRESS_LAUNCHD_PREFIXES = (
    "com.apple.",
    "com.google.",
    "com.huion.",
    "com.splashtop.",
    "com.valvesoftware.",
)

SUPPRESS_PORT_PROCESSES = frozenset(
    {
        "ControlCe",
        "rapportd",
        "ARDAgent",
        "LogiPlugi",
        "logioptio",
        "IPNExtens",
        "Plaud",
        "Spotify",
        "Slack",
        "Arc",
        "Linear",
        "Figma",
        "Raycast",
        "Discord",
        "Cursor",
        "Code",
        "Electron",
        "Postman",
        "Obsidian",
        "Notion",
        "Google",
        "Dropbox",
    }
)

SUPPRESS_PORTS_EXACT = frozenset({22, 111, 5900, 3389, 2019})

SUPPRESS_PORT_RANGES = [
    (33000, 65535),
]


def is_suppressed_systemd(unit_name: str) -> bool:
    if unit_name in SUPPRESS_SYSTEMD_EXACT:
        return True
    return any(unit_name.startswith(p) for p in SUPPRESS_SYSTEMD_PREFIXES)


def is_suppressed_launchd(label: str) -> bool:
    return any(label.startswith(p) for p in SUPPRESS_LAUNCHD_PREFIXES)


def is_suppressed_port(process_name: str, port: int) -> bool:
    if process_name in SUPPRESS_PORT_PROCESSES:
        return True
    if port in SUPPRESS_PORTS_EXACT:
        return True
    return any(lo <= port <= hi for lo, hi in SUPPRESS_PORT_RANGES)


def filter_services(services: Sequence[dict]) -> list[dict]:
    result = []
    for svc in services:
        stype = svc.get("type", "")
        name = svc.get("name", "")

        if stype == "systemd" and is_suppressed_systemd(name):
            continue
        if stype == "launchd" and is_suppressed_launchd(name):
            continue
        if stype == "port":
            proc = svc.get("process", "")
            port = svc.get("port", 0)
            if is_suppressed_port(proc, port):
                continue

        result.append(svc)
    return result
