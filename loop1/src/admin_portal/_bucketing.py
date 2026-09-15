"""Shared day-bucketing / zero-fill helper for admin portal timeseries
endpoints. Extracted here rather than written once per router — the Users
analytics endpoints (users_analytics.py) are the first consumer, but this is
generic (any number of named per-day count series) so a future timeseries
endpoint reuses it instead of re-deriving the same zero-fill logic.

Day boundaries are computed in the caller (each caller decides naive vs
aware, which window), so this module only ever deals with plain `date`
objects and `date -> int` count dicts — it doesn't touch datetimes at all.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def daily_date_range(start: date, end: date) -> list[date]:
    """Every calendar date from start to end, inclusive."""
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def build_daily_series(start: date, end: date, **named_counts: dict[date, int]) -> list[dict[str, Any]]:
    """Zero-filled daily rows, one per date in [start, end], covering every
    key in named_counts — a day with zero rows still gets an entry (0), not
    an omission, so a chart's x-axis can't be misread as having a gap.

    named_counts: e.g. new_patients={date(2026,8,17): 2, ...}, active_patients={...}
    Returns: [{"date": "2026-08-17", "new_patients": 2, "active_patients": 0}, ...]
    """
    rows: list[dict[str, Any]] = []
    for d in daily_date_range(start, end):
        row: dict[str, Any] = {"date": d.isoformat()}
        for name, counts in named_counts.items():
            row[name] = counts.get(d, 0)
        rows.append(row)
    return rows


def to_date(value: Any) -> date:
    """Normalize a `func.date(...)` GROUP BY key to a plain `date` — SQLite
    returns a string ("2026-08-17"), Postgres returns a `date` object
    directly; this makes both usable as the same dict key."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
