"""Cartographer DB Persistence — stores scan results in ops.cartographer_scans.

Requires ODIN_DB_PASSWORD (see src/infra/db.py). Gracefully skips if DB is unreachable.
"""

import json
import logging
from typing import Optional

from ..infra import db

log = logging.getLogger("cartographer.persist")


def save_to_db(
    registry: dict,
    drift_counts: Optional[dict] = None,
    duration_ms: Optional[int] = None,
) -> bool:
    if not db.is_configured():
        log.debug("ODIN_DB_PASSWORD not set — skipping DB persistence")
        return False

    summary = registry.get("summary", {})
    services = registry.get("services", [])
    scan_errors = registry.get("scan_errors", {})

    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.cartographer_scans
                    (duration_ms, total, documented, undocumented,
                     healthy, unhealthy, not_found,
                     by_host, by_category, drift_counts,
                     scan_errors, services)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    duration_ms,
                    summary.get("total", 0),
                    summary.get("documented", 0),
                    summary.get("undocumented", 0),
                    summary.get("healthy", 0),
                    summary.get("unhealthy", 0),
                    summary.get("not_found", 0),
                    json.dumps(summary.get("by_host", {})),
                    json.dumps(summary.get("by_category", {})),
                    json.dumps(drift_counts) if drift_counts else None,
                    json.dumps(scan_errors),
                    json.dumps(services),
                ),
            )
        conn.close()
        log.info("Scan persisted to ops.cartographer_scans")
        return True
    except Exception as e:
        log.warning("Failed to persist scan to DB: %s", e)
        return False


def get_latest_scan() -> Optional[dict]:
    if not db.is_configured():
        return None

    try:
        conn = db.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT ops.get_cartographer_status()")
            row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return None
    except Exception as e:
        log.warning("Failed to query cartographer status: %s", e)
        return None
