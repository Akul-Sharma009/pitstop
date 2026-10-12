"""Smoke test for the vLLM endpoint: is it up, and does it call tools?

Usage: python scripts/check_vllm.py   (reads VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL)
"""

from __future__ import annotations

import json
import os
import sys
import time

from openai import OpenAI

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


def main() -> int:
    client = OpenAI(base_url=os.environ["VLLM_BASE_URL"], api_key=os.environ.get("VLLM_API_KEY", "EMPTY"))
    model = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B")

    served = [m.id for m in client.models.list().data]
    print(f"Models served: {served}")
    if model not in served:
        print(f"FAIL: {model} is not served; set VLLM_MODEL to one of the above.")
        return 1

    # LLM_NO_THINK=true: Qwen3's /no_think switch, for servers that ignore chat_template_kwargs (e.g. Ollama).
    no_think = " /no_think" if os.environ.get("LLM_NO_THINK", "false").lower() == "true" else ""
    start = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Is part EM-1004 in stock? Use the tool." + no_think}],
        tools=[TOOL],
        tool_choice="auto",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    calls = response.choices[0].message.tool_calls or []
    print(f"Answered in {time.time() - start:.1f}s")
    if not calls:
        print(f"FAIL: no tool call. Reply: {response.choices[0].message.content!r}")
        return 1
    args = json.loads(calls[0].function.arguments)
    print(f"OK: called {calls[0].function.name}({args})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
