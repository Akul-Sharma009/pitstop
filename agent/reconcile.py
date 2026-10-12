"""Fill a plan's facts from the tool trace instead of trusting the model to copy them.

The model supplies judgement (diagnosis, steps, part, summary, escalation reason). Order and booking IDs,
times, the engineer and stock come from what the tools actually returned. Only real conflicts between the
model's claims and the tools become warnings; omissions are simply filled in.
"""

from __future__ import annotations

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
    elif plan.get("purchase_order_id"):
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
        if not plan.get("escalation_reason"):
            plan["escalation_reason"] = _escalation_reason(trace)

    stock = next(
        (c for c in reversed(trace) if c.tool == "check_inventory" and _ok(c)
         and c.result.get("part_number") == plan.get("part_number")),
        None,
    )
    if stock:
        plan["part_in_stock"] = stock.result.get("quantity_on_hand", 0) > 0

    return plan, warnings


def _engineer_name(trace: list[ToolCall], engineer_id: str | None) -> str | None:
    for call in reversed(trace):
        if call.tool == "find_free_engineers" and _ok(call):
            for slot in call.result.get("slots", []):
                if slot.get("engineer_id") == engineer_id:
                    return slot.get("name")
    return None


def _escalation_reason(trace: list[ToolCall]) -> str:
    search = next((c for c in reversed(trace) if c.tool == "find_free_engineers" and _ok(c)), None)
    if search and not search.result.get("slots"):
        args = search.arguments
        return (
            f"No qualified engineer is free between {args.get('earliest_start')} and {args.get('latest_end')}. "
            "A manager needs to find cover or move the repair."
        )
    return "No engineer booking was made. A manager needs to schedule the repair."
