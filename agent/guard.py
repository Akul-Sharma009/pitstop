"""Guardrail checks on a finished plan. Findings become warnings the manager sees."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent.models import AgentResult, Alert


def _ok(call) -> bool:
    return isinstance(call.result, dict) and "error" not in call.result


def _in_stock(result: AgentResult) -> bool:
    return any(c.tool == "check_inventory" and _ok(c) and c.result.get("part_number") == result.part_number
               and c.result.get("quantity_on_hand", 0) > 0 for c in result.trace)


def _offers(result: AgentResult, order: dict) -> tuple[list[dict], dict | None]:
    """The offers from the last quote for the plan's part, and the one the order was placed with."""
    quote = next((c.result for c in reversed(result.trace) if c.tool == "get_supplier_quote" and _ok(c)
                  and c.result.get("part_number") == result.part_number), None)
    offers = (quote or {}).get("offers", [])
    return offers, next((o for o in offers if o.get("supplier") == order.get("supplier")), None)


def _delivery(offer: dict) -> datetime | None:
    return _aware(datetime.fromisoformat(offer["expected_delivery"])) if offer.get("expected_delivery") else None


def _late_supplier(result: AgentResult, order: dict, alert: Alert) -> list[str]:
    """An order delivered after the predicted failure can't prevent it, if another offer was in time."""
    failure = _aware(alert.predicted_failure_at)
    delivery = _delivery({"expected_delivery": order.get("expected_delivery")})
    if delivery is None or delivery < failure:
        return []
    offers, _ = _offers(result, order)
    in_time = [o for o in offers if (_delivery(o) or failure) < failure]
    if not in_time:
        return []
    best = in_time[0]  # offers come cheapest first
    return [f"{order.get('po_id')} from {order.get('supplier')} arrives after the predicted failure, "
            f"but {best['supplier']} ({best['currency']} {best['unit_price']:g}) would have arrived in time."]


def _dearer_supplier(result: AgentResult, order: dict, alert: Alert) -> list[str]:
    """The order should come from the cheapest offer that arrives in time to repair before the predicted failure
    (not only before the booked repair: a booking moved earlier must not justify a dearer supplier)."""
    offers, chosen = _offers(result, order)
    if not chosen:
        return []
    repair = (_aware(result.booking_end) - _aware(result.booking_start)) if result.booking_start and result.booking_end else None
    latest = _aware(alert.predicted_failure_at) - (repair or timedelta(0))
    cheaper = [o for o in offers if o.get("unit_price", 0) < chosen.get("unit_price", 0)
               and o.get("expected_delivery") and _delivery(o) <= latest]
    if not cheaper:
        return []
    best = min(cheaper, key=lambda o: o["unit_price"])
    return [f"{order.get('part_number')} was ordered from {chosen['supplier']} at {chosen['currency']} {chosen['unit_price']:g}, "
            f"but {best['supplier']} ({best['currency']} {best['unit_price']:g}) would have arrived in time to repair before the predicted failure."]


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
        for call in result.trace:  # a repair needs one part: a bigger order is a mistake or a manipulation
            if call.tool == "draft_purchase_order" and _ok(call) and int(call.result.get("quantity") or 1) > 1:
                warnings.append(f"{call.result.get('po_id')} orders {call.result.get('quantity')} units of "
                                f"{call.result.get('part_number')}; a repair needs 1. Check it before approving.")
        if orders:
            warnings += _late_supplier(result, orders[-1], alert)
        if orders:
            warnings += _dearer_supplier(result, orders[-1], alert)
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
