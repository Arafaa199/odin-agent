#!/usr/bin/env python3
"""Send a failure alert for a systemd service via Odin comms.

Rate-limited: one alert per service per hour. Prevents spam from
crash-looping services with Restart=always.

Includes direct Telegram API fallback for when odin-gateway is down,
since the normal comms path depends on the gateway shim.
"""

import datetime
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

STATE_PATH = Path.home() / ".odin" / ".failure-alert-state.json"
COOLDOWN_SECONDS = 3600

from src import comms
from src.config import load_settings

# Direct Telegram API fallback (no gateway dependency). Creds come from the
# environment (unit EnvironmentFile=.env) — never hardcode a live token
# in git: redaction is not revocation, history keeps it forever.
_TG_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_TG_API_URL = f"https://api.telegram.org/bot{_TG_BOT_TOKEN}/sendMessage"


def _send_telegram_direct(message: str) -> bool:
    """Fallback: send directly via Telegram Bot API using urllib (no deps)."""
    if not _TG_BOT_TOKEN or not _TG_CHAT_ID:
        print("No TELEGRAM_BOT_TOKEN/CHAT_ID in env — direct fallback unavailable", file=sys.stderr)
        return False
    payload = json.dumps(
        {
            "chat_id": _TG_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        }
    ).encode()
    req = urllib.request.Request(
        _TG_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Direct Telegram fallback also failed: {e}", file=sys.stderr)
        return False


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def main():
    service = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    now = datetime.datetime.now()
    now_ts = now.timestamp()

    from src.comms import mute as _mute

    if _mute.is_muted():
        print(f"[MUTED] failure alert suppressed: {service}")
        sys.exit(0)

    state = load_state()
    last_alert = state.get(service, 0)
    if now_ts - last_alert < COOLDOWN_SECONDS:
        print(f"Suppressed (cooldown): {service}")
        sys.exit(0)

    host = os.uname().nodename
    ts = now.strftime("%H:%M")

    comms.init_transports(load_settings())

    msg = (
        f"*Service Failure* \u2014 {host} {ts}\n\n"
        f"\u274c `{service}` failed\n\n"
        f"Check: `systemctl --user status {service}`"
    )

    sent = comms.send(msg)

    # Fallback: if primary comms failed (e.g. gateway down), try direct Telegram API
    if not sent:
        print("Primary comms failed, trying direct Telegram API fallback...")
        sent = _send_telegram_direct(msg)
        if sent:
            print("Sent via direct Telegram fallback")

    if sent:
        state = dict(state)
        state[service] = now_ts
        save_state(state)

    status = "sent" if sent else "FAILED"
    print(f"Alert {status} for {service}")
    sys.exit(0)


if __name__ == "__main__":
    main()
