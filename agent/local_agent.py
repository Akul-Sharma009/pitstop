"""Local agent backend: a tool-calling loop against our vLLM endpoint (OpenAI-compatible API).

Used for development and as the fallback when Evolus is unavailable. Same tools as the MCP server,
plus search_manual (the Evolus knowledge library's local stand-in) and submit_plan.
"""

from __future__ import annotations

import json
import os
from typing import Any

from agent import manual
from agent.models import Alert, ToolCall
from agent.prompts import SEARCH_MANUAL_SPEC, SUBMIT_PLAN_SPEC, SYSTEM_PROMPT, alert_message, strip_thinking
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
        self.manual_parts: set[str] = set()

    def run(self, alert: Alert) -> tuple[dict[str, Any], list[ToolCall]]:
        """Return the submitted plan and the trace of tool calls."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + ("\n/no_think" if self.no_think else "")},
            {"role": "user", "content": alert_message(alert, self.no_think)},
        ]
        trace: list[ToolCall] = []
        self.manual_parts = set()
        nudged = False

        for _ in range(MAX_STEPS):
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
        return call_tool(self.factory, name, arguments)
