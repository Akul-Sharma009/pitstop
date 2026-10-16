"""Evolus agent backend: our app drives the Evolus agent through its OpenAI-compatible agent chat.

API (evolus.ai/en/developers): base URL https://api.evolus.ai/v1, header Authorization: Bearer <application
API key>, model "<applicationId>:<agentId>" (GET /v1/models lists them). Messages are text; model settings
come from the agent. Each request without X-Conversation-Id starts a new conversation: one per alert.

The Evolus agent runs on our model, has the manual in a knowledge library and reaches the factory tools
through our MCP server. Its instructions are agent.prompts.SYSTEM_PROMPT. The reply is text only, so the
trace comes from the mock factory's call log (agent/factory_log.py) and is checked like a local run.

Fleet mode: the MCP tool calls don't say which machine they are for, so runs are queued one at a time and
the factory is told the machine first (its focus). That keeps each run's trace and stock to its own machine.

Backup model: EVOLUS_FALLBACK_AGENT_ID is a copy of the agent on another model (Gemma 4 primary, Qwen3.8
backup, both on AMD's Token Factory). If the primary's model can't be reached, or Evolus doesn't answer within
EVOLUS_TIMEOUT_S, and the failed attempt drafted nothing in the factory, the same alert runs on the backup and
the plan says so (`note`). If it already drafted an order or booking, a second run would draft a second one:
the plan fails instead and the manager decides.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from agent.factory_log import FactoryLog
from agent.models import Alert, Replan
from agent.prompts import alert_message, strip_thinking


EVOLUS_BASE_URL = "https://api.evolus.ai/v1"
RUN_LOCK = threading.Lock()  # one Evolus run at a time (see the module docstring)


class EvolusError(RuntimeError):
    pass


def evolus_client() -> Any:
    from openai import OpenAI

    # No client retries: an unavailable model goes to the backup agent instead (EvolusAgent.run).
    return OpenAI(base_url=os.environ.get("EVOLUS_BASE_URL") or EVOLUS_BASE_URL, api_key=os.environ["EVOLUS_API_KEY"],
                  max_retries=0)


def unavailable(exc: Exception) -> bool:
    """The agent's model couldn't answer (not a bad plan): Evolus's own_models errors, an upstream 5xx such as
    the Token Factory's "No available workers", or no answer in time."""
    from agent.runner import model_unreachable

    text = str(exc)
    status = getattr(exc, "status_code", None)
    return (model_unreachable(exc) or type(exc).__name__ in ("APITimeoutError", "Timeout", "ReadTimeout")
            or (isinstance(status, int) and status >= 500)
            or any(word in text for word in ("own_models", "no_available_workers", "Service Unavailable", "upstream")))


# What we ask Evolus to reply with. An example object, not a JSON Schema: given a schema, the model filled in
# the schema's own outline ({"type": "object", "properties": {...plan...}}).
PLAN_EXAMPLE = {
    "status": "awaiting_approval or escalated",
    "diagnosis": "what is wrong, in one sentence",
    "repair_steps": ["every repair step from the manual, in order, one per item"],
    "part_number": "part number from the manual, or null if the manual lists none",
    "part_name": "part name",
    "part_in_stock": False,
    "purchase_order_id": "PO-... from draft_purchase_order, or null",
    "expected_delivery": "ISO time from the order, or null",
    "booking_id": "BK-... from draft_booking, or null",
    "engineer_name": "engineer from find_free_engineers, or null",
    "booking_start": "ISO time, or null",
    "booking_end": "ISO time, or null",
    "escalation_reason": "why no engineer could be booked, or null",
    "operator_summary": "3-5 short plain sentences for the machine operator",
}


def extract_json(text: str) -> dict[str, Any]:
    """Return the last top-level JSON object in the text (models often wrap it in prose or a code fence).

    If the model echoed a JSON Schema outline around the plan, unwrap {"properties": {...}}.
    """
    decoder = json.JSONDecoder()
    found, start = None, 0
    while (start := text.find("{", start)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start += 1
            continue
        if isinstance(obj, dict):
            found = obj
        start = end
    if found is None:
        raise EvolusError("Evolus reply contained no JSON plan.")
    if "status" not in found and isinstance(found.get("properties"), dict):
        found = found["properties"]
    return found


def evolus_message(alert: Alert, replan: Replan | None = None) -> str:
    """The user message for one alert (and the manager's feedback, when replanning), then the JSON plan."""
    example = json.dumps(PLAN_EXAMPLE, indent=2)
    return (
        "Reply in English.\n\n"
        f"{alert_message(alert, replan=replan)}\n\n"
        "When you are done, reply in English with only the plan as one JSON object with these keys "
        f"(replace each description with the real value; use null where it says so):\n{example}"
    )


DRAFTS = ("draft_purchase_order", "draft_booking")


class EvolusAgent:
    def __init__(self, llm: Any = None, agent_id: str | None = None, factory_log: FactoryLog | None = None,
                 fallback_agent_id: str | None = None):
        self.llm = llm or evolus_client()
        self.agent_id = agent_id or os.environ["EVOLUS_AGENT_ID"]  # "<applicationId>:<agentId>"
        self.fallback_agent_id = fallback_agent_id if fallback_agent_id is not None else \
            os.environ.get("EVOLUS_FALLBACK_AGENT_ID") or None
        self.factory_log = factory_log or FactoryLog()
        self.timeout = float(os.environ.get("EVOLUS_TIMEOUT_S", 240))

    def _ask(self, agent_id: str, alert: Alert, replan: Replan | None) -> tuple[Any, list]:
        """One Evolus run; the factory calls it made come back even when it fails (attached to the error)."""
        self.factory_log.focus(alert.machine_id)
        start = self.factory_log.last()
        try:
            response = self.llm.chat.completions.create(
                model=agent_id, messages=[{"role": "user", "content": evolus_message(alert, replan)}],
                timeout=self.timeout)
        except Exception as exc:
            exc.calls = self.factory_log.calls_after(start)  # type: ignore[attr-defined]
            raise
        return response, self.factory_log.calls_after(start)

    def run(self, alert: Alert, replan: Replan | None = None) -> tuple[dict[str, Any], list]:
        note = None
        with RUN_LOCK:
            try:
                response, calls = self._ask(self.agent_id, alert, replan)
            except Exception as exc:
                drafted = [c for c in getattr(exc, "calls", []) if c.tool in DRAFTS
                           and isinstance(c.result, dict) and "error" not in c.result]
                if not (self.fallback_agent_id and unavailable(exc)) or drafted:
                    if drafted:
                        ids = ", ".join(str(c.result.get("po_id") or c.result.get("booking_id")) for c in drafted)
                        raise EvolusError(f"The AI model stopped answering in the middle of the plan, after drafting "
                                          f"{ids}. Reject those drafts and start again.") from exc
                    raise
                primary = os.environ.get("EVOLUS_AGENT_MODEL", "the main model")
                backup = os.environ.get("EVOLUS_FALLBACK_MODEL", "the backup model")
                note = f"Planned on the backup model ({backup}): {primary} was unavailable."
                response, calls = self._ask(self.fallback_agent_id, alert, replan)
        plan = extract_json(strip_thinking(response.choices[0].message.content or ""))
        if note:
            plan["note"] = note
        return plan, calls
