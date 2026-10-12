"""Guardrail checks on a finished plan. Findings become warnings the manager sees."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agent.models import AgentResult, Alert


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
        if result.purchase_order_id and not _seen_in_trace(result.purchase_order_id, result, {"draft_purchase_order"}):
            warnings.append(f"Purchase order {result.purchase_order_id} was not created by a tool call.")
        if result.booking_id and not _seen_in_trace(result.booking_id, result, {"draft_booking"}):
            warnings.append(f"Booking {result.booking_id} was not created by a tool call.")

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
