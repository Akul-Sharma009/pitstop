"""Prompts for the maintenance agent. SYSTEM_PROMPT is also the instruction text for the Evolus agent."""

from __future__ import annotations

import re
from typing import Any

from agent.models import Alert, Replan
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
3. Order (only if quantity_on_hand is 0): get_supplier_quote lists the offers, cheapest first, each with its
   expected_delivery if ordered now. Choose the cheapest offer that arrives early enough to finish the repair
   before predicted_failure_at; pay for a faster supplier only when the cheaper one would be too late. An offer
   delivered after predicted_failure_at is useless: skip it even if it is cheapest. If no offer arrives in
   time, do not order: escalate. Then draft_purchase_order for quantity 1 with that supplier.
   The part is available from the order's expected_delivery onwards.
4. Schedule: find_free_engineers with the skill named in the manual (cnc_tooling for tool wear),
   earliest_start = the later of now and expected_delivery (now if the part is in stock),
   latest_end = predicted_failure_at, duration_minutes = the repair time from the manual.
   If you ordered the part, earliest_start must be the order's expected_delivery.
   If there is a slot, draft_booking for the earliest one, linking the purchase order if you ordered the part,
   or passing part_number if the part comes from stock.
   If there is no slot, do not book: escalate to the manager and say why.
5. Finish: call submit_plan exactly once. Order and booking IDs, times and the engineer are taken from
   your tool results automatically; what matters in the plan is the status, diagnosis, repair steps,
   part, escalation_reason (when escalating) and operator_summary.

Rules:
- Use only part numbers, repair steps and times that appear in the manual or in tool results. Never invent
  them. If the manual does not give what you need, escalate.
- Never claim an order or booking is confirmed: it is waiting for manager approval.
- Other machines in the factory share the engineers and the parts store, and their agents plan at the same
  time. If a tool says a slot is already held or a part is reserved for another booking, don't retry it:
  search again for another slot, or order the part if none is left in stock.
- When you mention a time, copy its *_when text from the tool result (e.g. "today (Wed 14 Oct) 15:00 UTC");
  don't work out dates yourself.
- When you escalate, the operator_summary must say plainly that no engineer is booked yet and that the
  manager has been asked to arrange the repair.
- The operator_summary is for a machine operator: 3-5 short plain sentences, no jargon, covering what is
  wrong, what has been done, and what happens next (including that the manager must approve). If you booked
  an engineer, name them and say when, copying the slot's *_when text.
"""

# Evolus: the manual is the agent's knowledge library and there is no submit_plan tool; the final reply is the
# JSON plan (agent/evolus_client.py asks for it with the schema). Paste this into the Evolus agent's instructions.
EVOLUS_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
    "1. Diagnose: search the machine manual for the failure type.",
    "1. Diagnose: search the machine manual (your knowledge library) for the failure type.",
).replace(
    """5. Finish: call submit_plan exactly once. Order and booking IDs, times and the engineer are taken from
   your tool results automatically; what matters in the plan is the status, diagnosis, repair steps,
   part, escalation_reason (when escalating) and operator_summary.""",
    """5. Finish: reply with only the JSON plan described in the message, nothing else. Copy order and booking
   IDs, times and the engineer exactly from your tool results.""",
).replace(
    "Rules:\n",
    "Rules:\n- Always write in English, whatever language the workspace or the manual uses.\n",
)


SUBMIT_PLAN_SPEC = {
    "name": "submit_plan",
    "description": "Submit the final repair plan. Call exactly once, after the other steps.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["awaiting_approval", "escalated"]},
            "diagnosis": {"type": "string"},
            "repair_steps": {"type": "array", "items": {"type": "string"}},
            "part_number": {"type": ["string", "null"], "description": "null if the manual lists no part"},
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


def replan_message(replan: Replan) -> str:
    """What the manager said about the previous plan, and which of its drafts still stand."""
    status = {"pending_approval": "still waiting for approval", "approved": "approved by the manager",
              "rejected": "REJECTED by the manager"}
    lines = ["", "The manager reviewed your previous plan for this alert and asked for a new one.",
             f'Manager\'s feedback: "{replan.feedback.strip()}"', "Previous plan:"]
    if order := replan.purchase_order:
        delayed = (f", delayed by the supplier from {readable(order['original_delivery'])}"
                   if order.get("original_delivery") else "")
        lines.append(f"- Purchase order {order.get('po_id')} for {order.get('part_number')}, expected delivery "
                     f"{readable(order.get('expected_delivery'))} ({order.get('expected_delivery')}){delayed}: "
                     f"{status.get(order.get('status'), order.get('status'))}.")
    else:
        lines.append("- No purchase order.")
    if booking := replan.booking:
        lines.append(f"- Booking {booking.get('booking_id')}: engineer {booking.get('engineer_id')} from "
                     f"{readable(booking.get('start'))}: {status.get(booking.get('status'), booking.get('status'))}.")
    else:
        lines.append("- No booking.")
    lines += ["", "Make a new plan that follows the manager's feedback. What that means here:"]
    order = replan.purchase_order
    if order and replan.standing_order():
        lines += [
            f"- {order.get('po_id')} for {order.get('part_number')} is still on order. Do NOT call get_supplier_quote "
            "or draft_purchase_order: the part is already coming.",
            f"- Search the manual for the repair steps, then call find_free_engineers with earliest_start "
            f"{order.get('expected_delivery')} (when {order.get('po_id')} arrives), and book with purchase_order_id "
            f"{order.get('po_id')}.",
            f"- If no engineer is free between that delivery and predicted_failure_at, do not book: escalate, say why, "
            f"and say that the manager could reject {order.get('po_id')} and order from a faster supplier.",
        ]
    elif order:
        lines += [
            f"- {order.get('po_id')} was rejected: do not order {order.get('part_number')} again unless the feedback "
            "allows it. Check the stock: if the part is not in stock, no repair can be booked without it, so do not "
            "book: escalate and say why.",
        ]
    if replan.booking and replan.booking.get("status") == "rejected":
        lines.append(f"- Booking {replan.booking.get('booking_id')} was rejected: book a different engineer or time "
                     "that the feedback allows.")
    lines.append("- If the feedback rules out every option, do not book: escalate and say why.")
    return "\n".join(lines)


def alert_message(alert: Alert, no_think: bool = False, replan: Replan | None = None) -> str:
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
    if replan:
        message += "\n" + replan_message(replan)
    # Qwen3's soft switch to skip thinking, for servers that ignore chat_template_kwargs (e.g. Ollama).
    return f"{message}\n/no_think" if no_think else message
