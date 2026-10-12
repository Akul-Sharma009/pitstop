"""Local agent backend: a tool-calling loop against our vLLM endpoint (OpenAI-compatible API).

Used for development and as the fallback when Evolus is unavailable. Same tools as the MCP server,
plus search_manual (the Evolus knowledge library's local stand-in) and submit_plan.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from agent import manual
from agent.models import Alert, ToolCall
from agent.times import readable, utc, when
from agent.prompts import SEARCH_MANUAL_SPEC, SUBMIT_PLAN_SPEC, SYSTEM_PROMPT, alert_message, reasoning_of, strip_thinking
from mcp_server.factory import FactoryClient
from mcp_server.tools import TOOL_SPECS, call_tool

MAX_STEPS = 15
TOOLS = [{"type": "function", "function": spec} for spec in [SEARCH_MANUAL_SPEC, *TOOL_SPECS, SUBMIT_PLAN_SPEC]]


def vllm_client() -> Any:
    from openai import OpenAI

    return OpenAI(base_url=os.environ["VLLM_BASE_URL"], api_key=os.environ.get("VLLM_API_KEY", "EMPTY"))


class AgentError(RuntimeError):
    pass


class LocalAgent:
    def __init__(self, llm: Any = None, factory: FactoryClient | None = None, manual_sections=None, model=None):
        self.llm = llm or vllm_client()
        self.factory = factory or FactoryClient()
        self.sections = manual_sections if manual_sections is not None else manual.load_sections()
        self.model = model or os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B")
        # Qwen3 thinks before answering by default; off keeps tool loops fast. Ignored by other models.
        thinking = os.environ.get("VLLM_ENABLE_THINKING", "false").lower() == "true"
        self.extra_body = {"chat_template_kwargs": {"enable_thinking": thinking}}
        self.no_think = os.environ.get("LLM_NO_THINK", "false").lower() == "true"
        if self.no_think:
            self.extra_body["think"] = False  # Ollama's own switch; Ollama ignores chat_template_kwargs
        # Extra request fields, e.g. LLM_EXTRA_BODY='{"reasoning_effort": "none"}' (see check_vllm --thinking-probe).
        self.extra_body.update(json.loads(os.environ.get("LLM_EXTRA_BODY") or "{}"))
        self.manual_parts: set[str] = set()
        self.now: Any = None
        self.delivery: str | None = None
        self.transcript: list[dict[str, Any]] = []  # one entry per model reply, for debugging

    def run(self, alert: Alert) -> tuple[dict[str, Any], list[ToolCall]]:
        """Return the submitted plan and the trace of tool calls."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + ("\n/no_think" if self.no_think else "")},
            {"role": "user", "content": alert_message(alert, self.no_think)},
        ]
        trace: list[ToolCall] = []
        self.now = alert.detected_at
        self.delivery: str | None = None  # expected_delivery of the drafted order, once there is one
        self.manual_parts = set()
        self.transcript = []
        nudged = False

        for _ in range(MAX_STEPS):
            started = time.monotonic()
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                extra_body=self.extra_body,
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            content = strip_thinking(message.content)
            self.transcript.append({
                "seconds": round(time.monotonic() - started, 1),
                "text": content,
                "reasoning": reasoning_of(message),
                "tool_calls": [{"name": c.function.name, "arguments": c.function.arguments} for c in tool_calls],
            })
            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                        for c in tool_calls
                    ],
                }
                if tool_calls
                else {"role": "assistant", "content": content}
            )

            if not tool_calls:
                if nudged:
                    raise AgentError("Model stopped without calling submit_plan.")
                nudged = True
                messages.append({"role": "user", "content": "Continue with the tools, then call submit_plan."})
                continue

            # Run every other tool call in the turn before accepting a plan submitted alongside them.
            plan = None
            for call in tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments, result = {}, {"error": "arguments were not valid JSON"}
                else:
                    if name == "submit_plan":
                        plan = arguments
                        result = {"status": "received"}
                    else:
                        result = self._execute(name, arguments)
                if name != "submit_plan":
                    trace.append(ToolCall(tool=name, arguments=arguments, result=result))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})
            if plan is not None:
                return plan, trace

        raise AgentError(f"No plan after {MAX_STEPS} steps.")

    def _execute(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "search_manual":
            hits = manual.search(str(arguments.get("query", "")), self.sections)
            for hit in hits:
                self.manual_parts.update(manual.part_numbers(hit["text"]))
            return hits or {"error": "no matching section"}
        if name == "draft_purchase_order":
            part = str(arguments.get("part_number", ""))
            if part not in self.manual_parts:
                return {"error": f"Part {part} is not in the machine manual. Search the manual and use the part number it gives."}
        too_early = self._before_delivery(name, arguments)
        if too_early:
            return {"error": too_early}
        result = call_tool(self.factory, name, arguments)
        if name == "draft_purchase_order" and isinstance(result, dict) and "error" not in result:
            self.delivery = result.get("expected_delivery")
        return self._label_times(result)

    def _before_delivery(self, name: str, arguments: dict[str, Any]) -> str | None:
        """The repair can't start before the ordered part arrives: refuse, so the model searches again."""
        field = {"find_free_engineers": "earliest_start", "draft_booking": "start"}.get(name)
        if not field or not self.delivery:
            return None
        start, delivery = utc(arguments.get(field)), utc(self.delivery)
        if start is None or start >= delivery:
            return None
        verb = "Search with earliest_start" if name == "find_free_engineers" else "Book a slot starting at or after"
        return f"The ordered part arrives {readable(delivery)}. {verb} {delivery.isoformat()}."

    def _label_times(self, result: Any) -> Any:
        """Add 'when' wording next to times, so the model quotes 'today'/'tomorrow' instead of working it out."""
        if not isinstance(result, dict) or "error" in result:
            return result
        result = dict(result)
        for key in ("expected_delivery", "start"):
            if key in result and when(result[key], self.now):
                result[f"{key}_when"] = when(result[key], self.now)
        if isinstance(result.get("slots"), list):
            result["slots"] = [{**slot, "slot_start_when": when(slot.get("slot_start"), self.now)} for slot in result["slots"]]
        return result
