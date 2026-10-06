"""Postgres connection settings shared by the task runner, watchdog and cartographer.

All writers treat the database as optional: no ODIN_DB_PASSWORD means skip.
"""

from __future__ import annotations

import os


def is_configured() -> bool:
    return bool(os.environ.get("ODIN_DB_PASSWORD"))


def connect():
    import psycopg2

    return psycopg2.connect(
        host=os.environ.get("ODIN_DB_HOST", "localhost"),
        port=int(os.environ.get("ODIN_DB_PORT", "5432")),
        dbname=os.environ.get("ODIN_DB_NAME", "odin"),
        user=os.environ.get("ODIN_DB_USER", "odin"),
        password=os.environ.get("ODIN_DB_PASSWORD", ""),
    )
