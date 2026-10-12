"""Evolus agent backend: our app drives the Evolus agent through its OpenAI-compatible endpoint.

The Evolus agent runs on our vLLM model on AMD, has the manual in a knowledge library and the factory
tools through the MCP connector (mcp_server). Its instructions are agent.prompts.SYSTEM_PROMPT.

TODO(evolus day): confirm the endpoint path, auth header and how an agent is addressed (we assume
model = EVOLUS_AGENT_ID) against the Evolus developer docs, and whether the reply exposes tool calls.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent.models import Alert
from agent.prompts import SUBMIT_PLAN_SPEC, alert_message, strip_thinking


class EvolusError(RuntimeError):
    pass


def evolus_client() -> Any:
    from openai import OpenAI

    return OpenAI(base_url=os.environ["EVOLUS_BASE_URL"], api_key=os.environ["EVOLUS_API_KEY"])


def extract_json(text: str) -> dict[str, Any]:
    """Return the last JSON object in the text (models often wrap it in prose or a code fence)."""
    decoder = json.JSONDecoder()
    found = None
    for start, char in enumerate(text):
        if char == "{":
            try:
                obj, _ = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                found = obj
    if found is None:
        raise EvolusError("Evolus reply contained no JSON plan.")
    return found


class EvolusAgent:
    def __init__(self, llm: Any = None, agent_id: str | None = None):
        self.llm = llm or evolus_client()
        self.agent_id = agent_id or os.environ["EVOLUS_AGENT_ID"]

    def run(self, alert: Alert) -> tuple[dict[str, Any], list]:
        schema = json.dumps(SUBMIT_PLAN_SPEC["parameters"])
        prompt = (
            f"{alert_message(alert)}\n\n"
            f"When you are done, reply with only a JSON object matching this schema:\n{schema}"
        )
        response = self.llm.chat.completions.create(
            model=self.agent_id,
            messages=[{"role": "user", "content": prompt}],
        )
        return extract_json(strip_thinking(response.choices[0].message.content)), []
