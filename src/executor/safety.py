from __future__ import annotations

import logging
import re

log = logging.getLogger("safety")

PROD_PATTERN = re.compile(
    r"\bprd\b|production|prod[^u]|live|deploy.*prod|migrate.*prod",
    re.IGNORECASE,
)

# Personal domain: critical infrastructure patterns
PERSONAL_CRITICAL_PATTERN = re.compile(
    r"\bvpn\b|db-host|nas\b|homeassistant|home.assistant|n8n|postgres|docker.*restart|"
    r"docker.*stop|docker.*rm|systemctl|caddy|wazuh|pihole|certbot|iptables|ufw",
    re.IGNORECASE,
)

WRITE_PATTERN = re.compile(
    r"deploy|restart|config.*change|update.*prod|write|modify|alter|drop|delete|INSERT|UPDATE",
    re.IGNORECASE,
)

# Risk classification patterns (priority: DESTRUCTIVE > BACKUP_FIRST > CONFIRM_APPROACH > SAFE)
DESTRUCTIVE_PATTERN = re.compile(
    r"delete|remove\b|rm\b|drop\b|truncate|overwrite|replace\b|reset\b|force|purge|"
    r"clean\b|wipe|destroy|rollback|revert|undo|kill\b|stop.*service|uninstall",
    re.IGNORECASE,
)

BACKUP_PATTERN = re.compile(
    r"update|modify|change\b|edit\b|fix\b|patch|refactor|migrate|deploy|install|"
    r"configure|set.?up|upgrade|alter\b|rename|move\b|commit|push\b|merge",
    re.IGNORECASE,
)

CONFIRM_PATTERN = re.compile(
    r"investigate|debug|troubleshoot|analyze|review|optimize|improve|"
    r"restructure|redesign|architect|plan\b|assess|audit|evaluate",
    re.IGNORECASE,
)

SAFE_PATTERN = re.compile(
    r"create|add\b|echo|write.*file|touch|mkdir|check|verify|test\b|list\b|show|"
    r"read\b|view|print|log\b|status|health|ping|uptime|df\b|free\b|cat\b|grep|ls\b",
    re.IGNORECASE,
)


def detect_tier(description: str, tags: list[str], domain: str = "work") -> str:
    combined = f"{description} {','.join(tags)}"
    # Work domain: production server patterns
    if PROD_PATTERN.search(combined):
        if "approved" in tags:
            return "PROD_WRITE"
        return "PROD_READONLY"
    # Personal domain: critical homelab infrastructure patterns
    if domain == "personal" and PERSONAL_CRITICAL_PATTERN.search(combined):
        if "approved" in tags:
            return "PROD_WRITE"
        return "PROD_READONLY"
    return "AUTO"


def needs_approval(description: str, tier: str) -> bool:
    if tier != "PROD_READONLY":
        return False
    return bool(WRITE_PATTERN.search(description))


def safety_rules(tier: str) -> str:
    if tier == "AUTO":
        return """## Safety: AUTO TIER
Full access to staging/testing instances and local repos.
- SSH to any staging/testing instance: allowed
- Code changes in the project's working repo: allowed (commit after)
- Local scripts, data analysis, docs: allowed
- Production SSH: NOT ALLOWED in this tier
- Production writes: NOT ALLOWED in this tier"""

    if tier == "PROD_READONLY":
        return """## Safety: PROD_READONLY TIER
Production access is READ-ONLY. No writes, no restarts, no config changes.
- SSH to production: ALLOWED (read-only commands only)
- Allowed commands: cat, grep, tail, less, head, ls, df, free, top, ps, pg_dump, SELECT
- FORBIDDEN: service restart, config edit, rm, mv, cp (to prod), INSERT, UPDATE, DELETE, ALTER, DROP
- FORBIDDEN: deploy, pip install, apt install, systemctl, docker restart
- If task requires writes: annotate task with reason and skip"""

    if tier == "PROD_WRITE":
        return """## Safety: PROD_WRITE TIER (+approved)
Production write access granted. Proceed with extreme caution.
- Test on staging FIRST before touching production
- Document every change in state.md
- Take backups before any data modification
- One change at a time, verify between steps"""

    return ""


def classify_task_risk(description: str, tags: list[str]) -> str:
    text = f"{description} {','.join(tags)}"

    if DESTRUCTIVE_PATTERN.search(text):
        return "DESTRUCTIVE"
    if BACKUP_PATTERN.search(text):
        return "BACKUP_FIRST"
    if CONFIRM_PATTERN.search(text):
        return "CONFIRM_APPROACH"
    if SAFE_PATTERN.search(text):
        return "SAFE"
    return "CONFIRM_APPROACH"


def requires_risk_approval(tier: str, risk: str) -> bool:
    """Combined tier + risk check. PROD_WRITE with destructive/risky ops always needs approval."""
    if tier == "PROD_WRITE" and risk in ("CONFIRM_APPROACH", "DESTRUCTIVE"):
        return True
    if risk in ("CONFIRM_APPROACH", "DESTRUCTIVE"):
        return True
    return False


BACKUP_RULES = """
## MANDATORY BACKUP RULE
Before modifying ANY file, create a timestamped backup:
  cp <file> <file>.bak.$(date +%Y%m%d-%H%M%S)
Before modifying ANY database table, take a pg_dump of affected tables.
Before modifying ANY server config, snapshot the current state.
Log all backup paths in your output."""
