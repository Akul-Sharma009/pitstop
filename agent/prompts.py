"""Prompts for the maintenance agent. SYSTEM_PROMPT is also the instruction text for the Evolus agent."""

from __future__ import annotations

import re
from typing import Any

from agent.models import Alert
from agent.times import hours_between, readable

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def strip_thinking(text: str | None) -> str:
    """Remove Qwen3 <think>...</think> blocks that some servers (e.g. Ollama) leave in the reply."""
    return _THINK.sub("", text or "").strip()


# Text longer than this before a tool call is the model thinking out loud, not a short status line.
THINKING_TEXT_MIN = 200


def thinking_chars(reasoning: str, text: str, called_tool: bool) -> tuple[int, int]:
    """(chars in the reasoning field or <think>, chars of thinking written as normal text before a tool call)."""
    return len(reasoning), len(text) if called_tool and len(text) > THINKING_TEXT_MIN else 0


def reasoning_of(message: Any) -> str:
    """The model's reasoning for one reply, wherever the server put it (field or <think> block)."""
    for field in ("reasoning", "reasoning_content"):
        value = getattr(message, field, None)
        if value:
            return str(value)
    return "\n".join(m.strip() for m in re.findall(r"<think>(.*?)</think>", message.content or "", re.DOTALL))


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
   If you ordered the part, earliest_start must be the order's expected_delivery.
   If there is a slot, draft_booking for the earliest one, linking the purchase order if any.
   If there is no slot, do not book: escalate to the manager and say why.
5. Finish: call submit_plan exactly once. Order and booking IDs, times and the engineer are taken from
   your tool results automatically; what matters in the plan is the status, diagnosis, repair steps,
   part, escalation_reason (when escalating) and operator_summary.

Rules:
- Use only part numbers, repair steps and times that appear in the manual or in tool results. Never invent
  them. If the manual does not give what you need, escalate.
- Never claim an order or booking is confirmed: it is waiting for manager approval.
- When you mention a time, copy its *_when text from the tool result (e.g. "today (Wed 14 Oct) 15:00 UTC");
  don't work out dates yourself.
- When you escalate, the operator_summary must say plainly that no engineer is booked yet and that the
  manager has been asked to arrange the repair.
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
        "required": ["status", "diagnosis", "repair_steps", "part_number", "escalation_reason", "operator_summary"],
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


def alert_message(alert: Alert, no_think: bool = False) -> str:
    lines = [
        "New failure warning. Plan the repair.",
        f"Now: {readable(alert.detected_at)} ({alert.detected_at.isoformat()}).",
        f"Predicted failure: {readable(alert.predicted_failure_at)}, "
        f"in about {hours_between(alert.detected_at, alert.predicted_failure_at):.0f} hours.",
    ]
    if alert.tool_wear_min is not None and alert.remaining_cutting_min is not None:
        limit = alert.tool_wear_min + alert.remaining_cutting_min
        lines.append(
            f"Tool wear: the tool has cut for {alert.tool_wear_min:.0f} minutes so far, below its wear limit of about "
            f"{limit:.0f} minutes, so about {alert.remaining_cutting_min:.0f} minutes of cutting are left. "
            "This is machine running time, not clock time."
        )
    message = "\n".join(lines) + f"\n\nAlert data:\n{alert.model_dump_json(indent=2)}"
    # Qwen3's soft switch to skip thinking, for servers that ignore chat_template_kwargs (e.g. Ollama).
    return f"{message}\n/no_think" if no_think else message
