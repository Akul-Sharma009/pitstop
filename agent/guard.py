"""Guardrail checks on a finished plan. Findings become warnings the manager sees."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.models import AgentResult, Alert


def _ok(call) -> bool:
    return isinstance(call.result, dict) and "error" not in call.result


def _in_stock(result: AgentResult) -> bool:
    return any(c.tool == "check_inventory" and _ok(c) and c.result.get("part_number") == result.part_number
               and c.result.get("quantity_on_hand", 0) > 0 for c in result.trace)


def _dearer_supplier(result: AgentResult, order: dict) -> list[str]:
    """The order should come from the cheapest offer that arrives before the booked repair."""
    quote = next((c.result for c in reversed(result.trace) if c.tool == "get_supplier_quote" and _ok(c)
                  and c.result.get("part_number") == result.part_number), None)
    chosen = next((o for o in (quote or {}).get("offers", []) if o.get("supplier") == order.get("supplier")), None)
    if not chosen:
        return []
    start = _aware(result.booking_start)
    cheaper = [o for o in quote["offers"] if o.get("unit_price", 0) < chosen.get("unit_price", 0)
               and o.get("expected_delivery") and _aware(datetime.fromisoformat(o["expected_delivery"])) <= start]
    if not cheaper:
        return []
    best = min(cheaper, key=lambda o: o["unit_price"])
    return [f"{order.get('part_number')} was ordered from {chosen['supplier']} at {chosen['currency']} {chosen['unit_price']:g}, "
            f"but {best['supplier']} ({best['currency']} {best['unit_price']:g}) would have arrived in time for the booked repair."]


def _seen_in_trace(value: str, result: AgentResult, tools: set[str]) -> bool:
    return any(call.tool in tools and value in json.dumps(call.result, default=str) for call in result.trace)


def check(result: AgentResult, alert: Alert) -> list[str]:
    warnings: list[str] = []

    if result.status == "awaiting_approval" and not result.booking_id:
        warnings.append("Plan is awaiting approval but has no engineer booking.")
    if result.status == "escalated" and not result.escalation_reason:
        warnings.append("Plan is escalated without a reason.")

    if not result.trace:
        warnings.append("No tool trace available: part number and drafts could not be verified.")
    else:
        if result.part_number and not _seen_in_trace(
            result.part_number, result, {"search_manual", "check_inventory", "get_supplier_quote"}
        ):
            warnings.append(f"Part number {result.part_number} was not found in the manual or the inventory.")
        for call in result.trace:
            if call.tool == "draft_purchase_order" and isinstance(call.result, dict) and "error" not in call.result:
                ordered = call.result.get("part_number")
                if ordered != result.part_number:
                    warnings.append(f"An order was drafted for part {ordered}, but the plan is for {result.part_number}.")
        if result.purchase_order_id and not _seen_in_trace(result.purchase_order_id, result, {"draft_purchase_order"}):
            warnings.append(f"Purchase order {result.purchase_order_id} was not created by a tool call.")
        if result.booking_id and not _seen_in_trace(result.booking_id, result, {"draft_booking"}):
            warnings.append(f"Booking {result.booking_id} was not created by a tool call.")
        orders = [c.result for c in result.trace if c.tool == "draft_purchase_order" and _ok(c)
                  and c.result.get("part_number") == result.part_number and c.result.get("status") != "rejected"]
        if len(orders) > 1:
            warnings.append(f"{len(orders)} orders were drafted for {result.part_number}: "
                            f"{', '.join(o.get('po_id', '?') for o in orders)}. Only one is needed.")
        if orders and result.booking_start:
            warnings += _dearer_supplier(result, orders[-1])
        if result.booking_id and result.part_number and not orders and not _in_stock(result):
            warnings.append(f"A repair is booked, but {result.part_number} is neither in stock nor on order.")

    if result.booking_start and result.booking_end:
        start, end = _aware(result.booking_start), _aware(result.booking_end)
        if result.expected_delivery and start < _aware(result.expected_delivery):
            warnings.append("Engineer is booked before the part is expected to arrive.")
        if start < _aware(alert.detected_at):
            warnings.append("Engineer is booked in the past.")
        if end > _aware(alert.predicted_failure_at):
            warnings.append("Repair is booked after the predicted failure.")

    return warnings


def _aware(value: datetime) -> datetime:
    """Treat times without a time zone as UTC so comparisons never fail."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
