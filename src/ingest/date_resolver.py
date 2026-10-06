"""Deterministic date resolution for relative dates in the ODIN_TIMEZONE zone (default UTC).

LLM outputs due_raw (exact words from transcript). This module resolves
to ISO date or None. Never guesses — returns None for anything unclear.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.environ.get("ODIN_TIMEZONE", "UTC"))

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_DAY_RE = re.compile(
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})",
    re.IGNORECASE,
)
MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def resolve(raw: str | None, recording_date: str) -> tuple[str | None, bool]:
    """Resolve a raw date expression to (ISO date or None, was_inferred).

    Returns (None, False) for anything unclear — forces confirmation.
    """
    if not raw:
        return None, False

    raw = raw.strip().lower()

    # Already ISO
    if ISO_DATE_RE.match(raw):
        return raw, False

    try:
        anchor = datetime.strptime(recording_date[:10], "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    except (ValueError, TypeError):
        return None, False

    # Simple relative
    if raw in ("today", "end of day", "eod"):
        return anchor.strftime("%Y-%m-%d"), True

    if raw in ("tomorrow", "tmrw"):
        return (anchor + timedelta(days=1)).strftime("%Y-%m-%d"), True

    if raw in ("end of week", "this week", "eow", "by end of week"):
        # Next Friday (or today if already Friday)
        days_until = (4 - anchor.weekday()) % 7
        if days_until == 0 and anchor.weekday() == 4:
            days_until = 0  # today is Friday
        elif days_until == 0:
            days_until = 7
        target = anchor + timedelta(days=days_until)
        return target.strftime("%Y-%m-%d"), True

    if raw in ("next week",):
        days_until_monday = (7 - anchor.weekday()) % 7 or 7
        return (anchor + timedelta(days=days_until_monday)).strftime("%Y-%m-%d"), True

    if raw in ("end of month", "eom", "by end of month"):
        # Last day of current month
        if anchor.month == 12:
            last = anchor.replace(year=anchor.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            last = anchor.replace(month=anchor.month + 1, day=1) - timedelta(days=1)
        return last.strftime("%Y-%m-%d"), True

    # "by Friday", "next Tuesday", "this Thursday"
    for prefix in ("by ", "next ", "this ", "before ", "on ", ""):
        for day_name, day_num in WEEKDAYS.items():
            if raw == prefix + day_name or raw == prefix + day_name + "?":
                delta = (day_num - anchor.weekday()) % 7
                if delta == 0:
                    # "by Friday" on a Friday = today; "next Friday" = next week
                    if prefix in ("next ",):
                        delta = 7
                    # else delta stays 0 → today (for "by", "this", "on", "before", "")
                elif "next" in prefix:
                    delta += 7  # skip this week
                return (anchor + timedelta(days=delta)).strftime("%Y-%m-%d"), True

    # "February 20", "Feb 20th", "March 1st"
    m = MONTH_DAY_RE.search(raw)
    if m:
        month_str = m.group(0)[:3].lower()
        month_num = MONTH_NAMES.get(month_str)
        day_num = int(m.group(1))
        if month_num and 1 <= day_num <= 31:
            year = anchor.year
            candidate = anchor.replace(month=month_num, day=min(day_num, 28))
            if candidate < anchor:
                year += 1
            try:
                resolved = datetime(year, month_num, day_num, tzinfo=LOCAL_TZ)
                return resolved.strftime("%Y-%m-%d"), False
            except ValueError:
                pass

    # Unresolvable
    return None, False


def confidence_for(raw: str | None) -> float:
    """Estimate how confident we are about the date interpretation."""
    if not raw:
        return 0.0
    raw = raw.strip().lower()
    if ISO_DATE_RE.match(raw):
        return 1.0
    if raw in ("today", "tomorrow", "tmrw"):
        return 0.95
    if MONTH_DAY_RE.search(raw):
        return 0.9
    if any(raw.startswith(p) for p in ("by ", "next ", "this ", "on ")):
        return 0.8
    if raw in ("end of week", "eow", "this week", "next week", "end of month", "eom"):
        return 0.75
    # vague
    return 0.3
