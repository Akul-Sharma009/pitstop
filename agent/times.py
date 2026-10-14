"""Time parsing and wording shared by the agent, reconciliation and the eval."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc(value: Any) -> datetime | None:
    """Parse an ISO time ('Z' or an offset; no zone means UTC) to an aware UTC datetime; None if it isn't one."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def readable(value: Any) -> str:
    """'2026-10-15T08:00:00Z' -> 'Thu 15 Oct 08:00 UTC'; anything unparseable is returned as is."""
    moment = utc(value)
    return f"{moment:%a} {moment.day} {moment:%b %H:%M} UTC" if moment else str(value)


def when(value: Any, now: Any) -> str | None:
    """Wording a model can quote: 'today (Wed 14 Oct) 15:00 UTC', 'tomorrow (…)', or 'Fri 16 Oct 08:00 UTC'."""
    moment, reference = utc(value), utc(now)
    if moment is None or reference is None:
        return None
    days = (moment.date() - reference.date()).days
    day = f"{moment:%a} {moment.day} {moment:%b}"
    if days == 0:
        return f"today ({day}) {moment:%H:%M} UTC"
    if days == 1:
        return f"tomorrow ({day}) {moment:%H:%M} UTC"
    return readable(moment)


def label_times(result: Any, now: Any) -> Any:
    """Add 'when' wording next to the times in a tool result, so a model quotes 'today'/'tomorrow'
    instead of working dates out (it gets them wrong)."""
    if not isinstance(result, dict) or "error" in result:
        return result
    result = dict(result)
    for key in ("expected_delivery", "start"):
        if key in result and when(result[key], now):
            result[f"{key}_when"] = when(result[key], now)
    if isinstance(result.get("slots"), list):
        result["slots"] = [{**slot, "slot_start_when": when(slot.get("slot_start"), now)} for slot in result["slots"]]
    return result


def hours_between(start: Any, end: Any) -> float | None:
    a, b = utc(start), utc(end)
    return None if a is None or b is None else (b - a).total_seconds() / 3600
