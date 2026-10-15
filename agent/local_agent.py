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
from agent.models import Alert, Replan, ToolCall
from agent.times import label_times, readable, utc
from agent.prompts import SEARCH_MANUAL_SPEC, SUBMIT_PLAN_SPEC, SYSTEM_PROMPT, alert_message, reasoning_of, strip_thinking
from mcp_server.factory import FactoryClient
from mcp_server.tools import TOOL_SPECS, call_tool

MAX_STEPS = 15
# Chosen 12 Oct: 6/6 on the scenario eval, ~14x faster per call than thinking Qwen3 (see CLAUDE.md).
DEFAULT_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"
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
        self.model = model or os.environ.get("VLLM_MODEL", DEFAULT_MODEL)
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

    def run(self, alert: Alert, replan: Replan | None = None) -> tuple[dict[str, Any], list[ToolCall]]:
        """Return the submitted plan and the trace of tool calls. With `replan`, plan again after the
        manager's feedback on the previous plan."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + ("\n/no_think" if self.no_think else "")},
            {"role": "user", "content": alert_message(alert, self.no_think, replan)},
        ]
        trace: list[ToolCall] = []
        self.now = alert.detected_at
        self.factory.machine_id = alert.machine_id  # fleet mode: whose orders and stock these calls are about
        standing = replan.standing_order() if replan else None
        # expected_delivery of the order the repair waits for: a standing one, or the one drafted in this run
        self.delivery: str | None = standing.get("expected_delivery") if standing else None
        self.out_of_stock: str | None = None  # a part check_inventory found with 0 on hand
        self.in_stock: str | None = None  # a part check_inventory found on hand (a booking reserves one)
        self.failure_at = utc(alert.predicted_failure_at)
        self.offers: dict[str, list[dict[str, Any]]] = {}  # part → offers from its last get_supplier_quote
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
            if not self.sections:
                return {"error": "The machine manual is not loaded (no .md files in MANUAL_DIR). Escalate to the manager."}
            hits = manual.search(str(arguments.get("query", "")), self.sections)
            for hit in hits:
                self.manual_parts.update(manual.part_numbers(hit["text"]))
            return hits or {"error": "no matching section"}
        if name == "draft_purchase_order":
            part = str(arguments.get("part_number", ""))
            if part not in self.manual_parts:
                return {"error": f"Part {part} is not in the machine manual. Search the manual and use the part number it gives."}
        if name == "draft_booking" and not arguments.get("purchase_order_id") and not arguments.get("part_number") \
                and self.in_stock and not self.delivery:
            arguments["part_number"] = self.in_stock  # from stock: the booking reserves it (shared with other machines)
        refused = (self._no_part(name) or self._before_delivery(name, arguments) or self._after_failure(name, arguments)
                   or self._too_late(name, arguments))
        if refused:
            return {"error": refused}
        result = call_tool(self.factory, name, arguments, now=self.now)
        ok = isinstance(result, dict) and "error" not in result
        if name == "draft_purchase_order" and ok:
            self.delivery = result.get("expected_delivery")
        if name == "get_supplier_quote" and ok:
            self.offers[result.get("part_number")] = result.get("offers", [])
        if name == "check_inventory" and ok:
            in_stock = result.get("quantity_on_hand", 0) > 0
            self.out_of_stock = None if in_stock else result.get("part_number")
            self.in_stock = result.get("part_number") if in_stock else None
        return self._label_times(result)

    def _no_part(self, name: str) -> str | None:
        """A repair needs its part: no engineer search or booking while it is neither in stock nor on order."""
        if name not in ("find_free_engineers", "draft_booking") or not self.out_of_stock or self.delivery:
            return None
        return (f"{self.out_of_stock} is not in stock and not on order, so no repair can be booked. Order it if that "
                "is allowed; otherwise escalate to the manager and say why.")

    def _too_late(self, name: str, arguments: dict[str, Any]) -> str | None:
        """An order delivered after the predicted failure can't prevent it: refuse it while another offer is in
        time. If no offer is in time, let it through (the plan escalates; the order is the manager's call)."""
        offers = self.offers.get(str(arguments.get("part_number"))) if name == "draft_purchase_order" else None
        if not offers or self.failure_at is None:
            return None
        wanted = arguments.get("supplier")
        chosen = next((o for o in offers if o.get("supplier") == wanted), None) if wanted else offers[0]
        delivery = utc(chosen.get("expected_delivery")) if chosen else None
        in_time = [o for o in offers if (utc(o.get("expected_delivery")) or self.failure_at) < self.failure_at]
        if delivery is None or not in_time:
            return None
        best = in_time[0]  # offers come cheapest first
        if delivery >= self.failure_at:
            return (f"{chosen['supplier']} delivers {readable(delivery)}, after the predicted failure "
                    f"({readable(self.failure_at)}), so the repair could not happen in time. {best['supplier']} delivers "
                    f"{readable(best['expected_delivery'])}: order from {best['supplier']}.")
        if chosen.get("unit_price", 0) > best.get("unit_price", 0):  # purchasing rule; also what a supplier note can't change
            return (f"{best['supplier']} is cheaper ({best.get('currency')} {best.get('unit_price'):g}) and also delivers "
                    f"before the predicted failure ({readable(best['expected_delivery'])}): order from {best['supplier']}.")
        return None

    def _after_failure(self, name: str, arguments: dict[str, Any]) -> str | None:
        """The repair must finish before the predicted failure: refuse a search or booking that ends later (the
        model has been seen moving the failure a day to make an empty window fit)."""
        field = {"find_free_engineers": "latest_end", "draft_booking": "end"}.get(name)
        end = utc(arguments.get(field)) if field else None
        if end is None or self.failure_at is None or end <= self.failure_at:
            return None
        return (f"{field} {arguments.get(field)} is after the predicted failure ({readable(self.failure_at)}). Use "
                f"{self.failure_at.isoformat()}; if no repair fits before it, escalate to the manager.")

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
        return label_times(result, self.now)  # relative to the alert, so pinned test dates read right
