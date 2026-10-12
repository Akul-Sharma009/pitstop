"""Prompts for the maintenance agent. SYSTEM_PROMPT is also the instruction text for the Evolus agent."""

from __future__ import annotations

from agent.models import Alert

SYSTEM_PROMPT = """\
You are Pitstop, a maintenance planning agent in a factory. A failure model has flagged a machine that is
about to fail. Turn the warning into a repair plan that a manager will approve. You only draft: every
purchase order and booking you create is pending until a manager approves it.

Work through these steps with your tools:
1. Diagnose: search the machine manual for the failure type. Find the repair steps, the replacement
   part number, and how long the repair takes.
2. Check stock: check_inventory for that part number.
3. Order (only if quantity_on_hand is 0): get_supplier_quote, then draft_purchase_order for quantity 1.
   The part is available from expected_delivery onwards.
4. Schedule: find_free_engineers with the skill named in the manual (cnc_tooling for tool wear),
   earliest_start = the later of now and expected_delivery (now if the part is in stock),
   latest_end = predicted_failure_at, duration_minutes = the repair time from the manual.
   If there is a slot, draft_booking for the earliest one, linking the purchase order if any.
   If there is no slot, do not book: escalate to the manager and say why.
5. Finish: call submit_plan exactly once.

Rules:
- Use only part numbers, repair steps and times that appear in the manual or in tool results. Never invent
  them. If the manual does not give what you need, escalate.
- Never claim an order or booking is confirmed: it is waiting for manager approval.
- The operator_summary is for a machine operator: 3-5 short plain sentences, no jargon, covering what is
  wrong, what has been done, and what happens next (including that the manager must approve).
"""

SUBMIT_PLAN_SPEC = {
    "name": "submit_plan",
    "description": "Submit the final repair plan. Call exactly once, after the other steps.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["awaiting_approval", "escalated"]},
            "diagnosis": {"type": "string"},
            "repair_steps": {"type": "array", "items": {"type": "string"}},
            "part_number": {"type": "string"},
            "part_name": {"type": "string"},
            "part_in_stock": {"type": "boolean"},
            "purchase_order_id": {"type": ["string", "null"]},
            "expected_delivery": {"type": ["string", "null"]},
            "booking_id": {"type": ["string", "null"]},
            "engineer_name": {"type": ["string", "null"]},
            "booking_start": {"type": ["string", "null"]},
            "booking_end": {"type": ["string", "null"]},
            "escalation_reason": {"type": ["string", "null"]},
            "operator_summary": {"type": "string"},
        },
        "required": ["status", "diagnosis", "repair_steps", "part_number", "operator_summary"],
    },
}

SEARCH_MANUAL_SPEC = {
    "name": "search_manual",
    "description": "Search the machine manual. Returns the best-matching sections with their text.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "e.g. 'tool wear end mill replacement'"}},
        "required": ["query"],
    },
}


def alert_message(alert: Alert) -> str:
    return (
        "New failure warning. Plan the repair.\n"
        f"now: {alert.detected_at.isoformat()}\n"
        f"{alert.model_dump_json(indent=2)}"
    )
