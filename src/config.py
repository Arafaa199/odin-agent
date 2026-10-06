"""Config file resolution.

Each config file has a committed `<name>.example.yaml` and an optional local
`<name>.yaml` (gitignored). The local file wins when present.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

DEFAULT_TAG_DOMAINS = {
    "health": ["health", "fitness"],
    "finance": ["finance", "money", "budget"],
    "infra": ["infra", "server", "docker"],
}


def config_path(name: str, env_var: str | None = None) -> Path:
    """Return config/<name>.yaml if it exists, else config/<name>.example.yaml."""
    if env_var and os.environ.get(env_var):
        return Path(os.environ[env_var]).expanduser()
    local = CONFIG_DIR / f"{name}.yaml"
    return local if local.exists() else CONFIG_DIR / f"{name}.example.yaml"


def load_yaml(path: Path) -> dict | list:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_settings() -> dict:
    data = load_yaml(config_path("settings", "ODIN_SETTINGS"))
    return data if isinstance(data, dict) else {}


def load_hosts() -> dict[str, dict]:
    data = load_yaml(config_path("hosts", "ODIN_HOSTS"))
    hosts = data.get("hosts", {}) if isinstance(data, dict) else {}
    return {name: (cfg or {}) for name, cfg in hosts.items()}


def intermittent_hosts() -> set[str]:
    return {name for name, cfg in load_hosts().items() if cfg.get("intermittent")}


def vocabulary(settings: dict | None = None) -> dict:
    """Controlled vocabulary shared by extraction, validation and task routing.

    projects: project/workstream names the extractor may assign (routed to "work")
    systems:  extra system names that make a short task description concrete
    tag_domains: domain -> tags, used for queue routing and the tag allow-list
    """
    if settings is None:
        settings = load_settings()
    vocab = settings.get("vocabulary", {}) or {}
    projects = [p.lower() for p in vocab.get("projects", [])]
    systems = [s.lower() for s in vocab.get("systems", [])]
    tag_domains = vocab.get("tag_domains") or DEFAULT_TAG_DOMAINS
    tag_domains = {d: [t.lower() for t in tags] for d, tags in tag_domains.items()}
    all_tags = sorted({*projects, *(t for tags in tag_domains.values() for t in tags)})
    return {
        "projects": projects,
        "systems": systems,
        "tag_domains": tag_domains,
        "allowed_tags": all_tags,
    }
