"""Smoke test for the model endpoint: is it up, and does it call tools?

Usage:
  python scripts/check_vllm.py                   one tool-call request (reads VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL)
  python scripts/check_vllm.py --thinking-probe  try each known "thinking off" switch and show which one works
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a plain script from the repo
from agent.local_agent import DEFAULT_MODEL  # noqa: E402
from agent.prompts import reasoning_of, strip_thinking, thinking_chars  # noqa: E402

TOOL = {
    "type": "function",
    "function": {
        "name": "check_inventory",
        "description": "Look up a spare part in the parts store.",
        "parameters": {
            "type": "object",
            "properties": {"part_number": {"type": "string"}},
            "required": ["part_number"],
        },
    },
}
QUESTION = "Is part EM-1004 in stock? Use the tool."


def probe_variants() -> list[tuple[str, str, dict[str, Any]]]:
    """(label, user message, extra request fields) for each way servers turn Qwen3 thinking off."""
    return [
        ("no switch", QUESTION, {}),
        ("think: false (Ollama)", QUESTION, {"think": False}),
        ('reasoning_effort: "none"', QUESTION, {"reasoning_effort": "none"}),
        ("enable_thinking: false (vLLM)", QUESTION, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("/no_think in message", QUESTION + " /no_think", {}),
    ]


def ask(client: OpenAI, model: str, message: str, extra_body: dict[str, Any]) -> tuple[float, Any]:
    start = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": message}],
        tools=[TOOL],
        tool_choice="auto",
        extra_body=extra_body,
    )
    return time.time() - start, response.choices[0].message


def describe_thinking(reply: Any) -> str:
    """'none', or where the thinking went: the reasoning field, or normal text before the tool call."""
    field, text = thinking_chars(reasoning_of(reply), strip_thinking(reply.content), bool(reply.tool_calls))
    parts = [f"{field} chars (field)"] * bool(field) + [f"{text} chars (text)"] * bool(text)
    return ", ".join(parts) or "none"


def thinking_probe(client: OpenAI, model: str) -> int:
    print(f"{'switch':32} {'time':>6}  {'thinking':>18}  tool call")
    for label, message, extra in probe_variants():
        try:
            seconds, reply = ask(client, model, message, extra)
        except Exception as exc:  # a server may reject a field it doesn't know
            print(f"{label:32} {'-':>6}  {'-':>18}  error: {str(exc)[:80]}")
            continue
        print(f"{label:32} {seconds:5.1f}s  {describe_thinking(reply):>18}  {'yes' if reply.tool_calls else 'NO'}")
    print("\nA switch works if it shows thinking 'none' (neither field nor text) and a tool call.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--thinking-probe", action="store_true", help="compare the thinking-off switches")
    args = parser.parse_args()

    client = OpenAI(base_url=os.environ["VLLM_BASE_URL"], api_key=os.environ.get("VLLM_API_KEY", "EMPTY"))
    model = os.environ.get("VLLM_MODEL", DEFAULT_MODEL)

    served = [m.id for m in client.models.list().data]
    print(f"Models served: {served}")
    if model not in served:
        print(f"FAIL: {model} is not served; set VLLM_MODEL to one of the above.")
        return 1
    if args.thinking_probe:
        return thinking_probe(client, model)

    # Same switches the agent sends (agent/local_agent.py): vLLM's, plus Ollama's with LLM_NO_THINK=true.
    no_think = os.environ.get("LLM_NO_THINK", "false").lower() == "true"
    extra: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": False}, **({"think": False} if no_think else {})}
    extra.update(json.loads(os.environ.get("LLM_EXTRA_BODY") or "{}"))
    seconds, message = ask(client, model, QUESTION + (" /no_think" if no_think else ""), extra)
    print(f"Answered in {seconds:.1f}s")
    thinking = describe_thinking(message)
    if thinking == "none":
        print("Thinking: none seen in the reply")
    else:
        print(f"Note: the model still thought before answering ({thinking}; slower). Try --thinking-probe.")
    if not message.tool_calls:
        print(f"FAIL: no tool call. Reply: {message.content!r}")
        return 1
    call = message.tool_calls[0]
    print(f"OK: called {call.function.name}({json.loads(call.function.arguments)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
