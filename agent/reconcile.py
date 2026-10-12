"""Fill a plan's facts from the tool trace instead of trusting the model to copy them.

The model supplies judgement (diagnosis, steps, part, summary, escalation reason). Order and booking IDs,
times, the engineer and stock come from what the tools actually returned. Only real conflicts between the
model's claims and the tools become warnings; omissions are simply filled in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.models import ToolCall


def _ok(call: ToolCall) -> bool:
    return isinstance(call.result, dict) and "error" not in call.result


def _last(trace: list[ToolCall], tool: str) -> ToolCall | None:
    return next((c for c in reversed(trace) if c.tool == tool and _ok(c)), None)


def _set(plan: dict[str, Any], key: str, value: Any, warnings: list[str], label: str) -> None:
    claimed = plan.get(key)
    if claimed not in (None, "") and str(claimed) != str(value):
        warnings.append(f"The agent reported {label} {claimed}, but the tools returned {value}; using {value}.")
    plan[key] = value


def reconcile(plan: dict[str, Any], trace: list[ToolCall]) -> tuple[dict[str, Any], list[str]]:
    plan, warnings = dict(plan), []

    order = _last(trace, "draft_purchase_order")
    if order:
        _set(plan, "purchase_order_id", order.result.get("po_id"), warnings, "purchase order")
        plan["expected_delivery"] = order.result.get("expected_delivery")
    else:
        if plan.get("purchase_order_id"):
            warnings.append(f"The agent reported purchase order {plan['purchase_order_id']}, but no order was drafted.")
        plan["purchase_order_id"] = plan["expected_delivery"] = None

    booking = _last(trace, "draft_booking")
    if booking:
        _set(plan, "booking_id", booking.result.get("booking_id"), warnings, "booking")
        plan["booking_start"] = booking.result.get("start")
        plan["booking_end"] = booking.result.get("end")
        plan["engineer_name"] = _engineer_name(trace, booking.result.get("engineer_id")) or plan.get("engineer_name")
        plan["status"] = "awaiting_approval"
    else:
        if plan.get("booking_id"):
            warnings.append(f"The agent reported booking {plan['booking_id']}, but no booking was drafted.")
        for key in ("booking_id", "booking_start", "booking_end", "engineer_name"):
            plan[key] = None
        plan["status"] = "escalated"
        no_slots = _no_slots_reason(trace)
        if not plan.get("escalation_reason"):
            plan["escalation_reason"] = no_slots or "No engineer booking was made."

    stock = next(
        (c for c in reversed(trace) if c.tool == "check_inventory" and _ok(c)
         and c.result.get("part_number") == plan.get("part_number")),
        None,
    )
    if stock:
        plan["part_in_stock"] = stock.result.get("quantity_on_hand", 0) > 0

    if plan["status"] == "escalated":
        # The operator must not be told a repair is coming when nobody is booked: say it from the facts.
        plan["operator_summary"] = _escalation_summary(plan, no_slots or plan["escalation_reason"])

    return plan, warnings


def readable(value: Any) -> str:
    """'2026-10-15T08:00:00Z' -> 'Thu 15 Oct 08:00 UTC'; anything unparseable is returned as is."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment
    return f"{moment:%a} {moment.day} {moment:%b %H:%M} UTC"


def _escalation_summary(plan: dict[str, Any], reason: str) -> str:
    sentences = []
    diagnosis = (plan.get("diagnosis") or "").strip()
    if diagnosis:
        sentences.append(diagnosis if diagnosis.endswith(".") else diagnosis + ".")
    part = plan.get("part_number")
    if plan.get("purchase_order_id"):
        sentences.append(
            f"A replacement part ({part}) has been ordered and should arrive {readable(plan.get('expected_delivery'))}; "
            "the order is waiting for your manager's approval."
        )
    elif plan.get("part_in_stock") and part:
        sentences.append(f"The replacement part ({part}) is in stock.")
    sentences.append(f"No repair is booked yet: {reason[0].lower() + reason[1:] if reason else 'no engineer could be booked.'}")
    sentences.append("Your manager has been asked to arrange the repair.")
    return " ".join(sentences)


def _engineer_name(trace: list[ToolCall], engineer_id: str | None) -> str | None:
    for call in reversed(trace):
        if call.tool == "find_free_engineers" and _ok(call):
            for slot in call.result.get("slots", []):
                if slot.get("engineer_id") == engineer_id:
                    return slot.get("name")
    return None


def _no_slots_reason(trace: list[ToolCall]) -> str | None:
    """'No qualified engineer is free between ...' when the last engineer search came back empty."""
    search = next((c for c in reversed(trace) if c.tool == "find_free_engineers" and _ok(c)), None)
    if search and not search.result.get("slots"):
        args = search.arguments
        return (
            f"No qualified engineer is free between {readable(args.get('earliest_start'))} "
            f"and {readable(args.get('latest_end'))}."
        )
    return None
