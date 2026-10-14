"""Evolus agent backend: our app drives the Evolus agent through its OpenAI-compatible agent chat.

API (evolus.ai/en/developers): base URL https://api.evolus.ai/v1, header Authorization: Bearer <application
API key>, model "<applicationId>:<agentId>" (GET /v1/models lists them). Messages are text; model settings
come from the agent. Each request without X-Conversation-Id starts a new conversation: one per alert.

The Evolus agent runs on our model, has the manual in a knowledge library and reaches the factory tools
through our MCP server. Its instructions are agent.prompts.SYSTEM_PROMPT. The reply is text only, so the
trace comes from the mock factory's call log (agent/factory_log.py) and is checked like a local run.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.factory_log import FactoryLog
from agent.models import Alert
from agent.prompts import alert_message, strip_thinking


EVOLUS_BASE_URL = "https://api.evolus.ai/v1"


class EvolusError(RuntimeError):
    pass


def evolus_client() -> Any:
    from openai import OpenAI

    return OpenAI(base_url=os.environ.get("EVOLUS_BASE_URL") or EVOLUS_BASE_URL, api_key=os.environ["EVOLUS_API_KEY"])


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


def evolus_message(alert: Alert) -> str:
    """The user message for one alert: the alert, then the JSON plan we want back."""
    example = json.dumps(PLAN_EXAMPLE, indent=2)
    return (
        f"{alert_message(alert)}\n\n"
        "When you are done, reply in English with only the plan as one JSON object with these keys "
        f"(replace each description with the real value; use null where it says so):\n{example}"
    )


class EvolusAgent:
    def __init__(self, llm: Any = None, agent_id: str | None = None, factory_log: FactoryLog | None = None):
        self.llm = llm or evolus_client()
        self.agent_id = agent_id or os.environ["EVOLUS_AGENT_ID"]  # "<applicationId>:<agentId>"
        self.factory_log = factory_log or FactoryLog()

    def run(self, alert: Alert) -> tuple[dict[str, Any], list]:
        start = self.factory_log.last()
        response = self.llm.chat.completions.create(
            model=self.agent_id,
            messages=[{"role": "user", "content": evolus_message(alert)}],
        )
        plan = extract_json(strip_thinking(response.choices[0].message.content or ""))
        return plan, self.factory_log.calls_after(start)
