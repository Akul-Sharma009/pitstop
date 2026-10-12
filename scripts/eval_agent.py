"""Run the real model (vLLM) through the three demo scenarios against fake factory systems.

Checks what the agent actually did in the factory systems, not only what it claims. Use it to pick the
model and tune the prompt before the mocks and Evolus are ready.

Usage: python -m scripts.eval_agent [--scenario out_of_stock|in_stock|no_engineer] [--repeat N] [-v]
Reads VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL. With -v, prints the model's messages and reasoning between
tool calls and writes each full run to eval-logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent import runner
from agent.local_agent import DEFAULT_MODEL, LocalAgent
from agent.models import AgentResult
from agent.prompts import thinking_chars
from agent.times import utc
from devtools.fakes import FAILURE, MANUAL, NOW, PART, SLOTS, FakeFactory, sample_alert

DELIVERY = NOW + timedelta(hours=24)  # the fake supplier's lead time
REPAIR_MINUTES = 30  # from the manual's Tool wear section
LOG_DIR = Path("eval-logs")


def check(scenario: str, result: AgentResult, factory: FakeFactory) -> list[str]:
    """Return the problems found; empty means the scenario passed."""
    problems = [f"warning: {w}" for w in result.warnings]
    if result.status == "failed":
        return problems or ["agent failed"]
    if result.part_number != PART:
        problems.append(f"wrong part {result.part_number!r}, expected {PART}")
    if not result.operator_summary:
        problems.append("no operator summary")
    bookings = list(factory.bookings.values())
    wrong = [o["part_number"] for o in factory.orders.values() if o["part_number"] != PART]
    if wrong:
        problems.append(f"ordered the wrong part: {', '.join(wrong)}")
    if len(factory.orders) > 1 or len(bookings) > 1:
        problems.append(f"drafted {len(factory.orders)} orders and {len(bookings)} bookings, expected at most 1 each")
    problems += check_search_window(scenario, result)

    if scenario == "out_of_stock":
        if len(factory.orders) != 1:
            problems.append(f"expected 1 purchase order, got {len(factory.orders)}")
        if result.status != "awaiting_approval" or len(bookings) != 1:
            problems.append(f"expected 1 booking awaiting approval, got {len(bookings)} ({result.status})")
        elif utc(bookings[0]["start"]) < DELIVERY:
            problems.append(f"booked at {bookings[0]['start']}, before the part arrives")
    elif scenario == "in_stock":
        if factory.orders:
            problems.append("ordered a part that is in stock")
        if result.status != "awaiting_approval" or len(bookings) != 1:
            problems.append(f"expected 1 booking awaiting approval, got {len(bookings)} ({result.status})")
        elif utc(bookings[0]["start"]) != utc(SLOTS["in_stock"][0]):
            problems.append(f"booked {bookings[0]['start']}, not the earliest slot {SLOTS['in_stock'][0]}")
    elif scenario == "no_engineer":
        if result.status != "escalated":
            problems.append(f"expected escalated, got {result.status}")
        if bookings:
            problems.append("booked an engineer although no slot fits")
    return problems


def check_search_window(scenario: str, result: AgentResult) -> list[str]:
    """The engineer search must cover delivery (or now, if in stock) → predicted failure, for the repair time.

    A wrong window can still produce the right-looking outcome (no slots → escalate), so check it directly.
    """
    searches = [c for c in result.trace if c.tool == "find_free_engineers"]
    if not searches:
        return ["never searched for an engineer"]
    args = searches[-1].arguments
    expected_start = NOW if scenario == "in_stock" else DELIVERY
    problems = []
    if utc(args.get("earliest_start")) != expected_start:
        problems.append(f"engineer search starts {args.get('earliest_start')}, expected {expected_start.isoformat()}")
    if utc(args.get("latest_end")) != FAILURE:
        problems.append(f"engineer search ends {args.get('latest_end')}, expected {FAILURE.isoformat()}")
    if args.get("duration_minutes") != REPAIR_MINUTES:
        problems.append(f"engineer search asks for {args.get('duration_minutes')} min, the manual says {REPAIR_MINUTES}")
    return problems


def speed(transcript: list[dict[str, Any]]) -> tuple[int, float, int]:
    """(model calls, total seconds in the model, chars of thinking) for one run."""
    seconds = sum(reply.get("seconds", 0) for reply in transcript)
    thinking = sum(
        sum(thinking_chars(reply["reasoning"], reply["text"], bool(reply["tool_calls"]))) for reply in transcript
    )
    return len(transcript), seconds, thinking


def print_run(agent: LocalAgent, result: AgentResult) -> None:
    """The model's text and reasoning for each reply, followed by the tool results it got back."""
    results = iter(result.trace)
    for turn, reply in enumerate(agent.transcript, 1):
        if reply["reasoning"]:
            print(f"     [{turn}] thinking: {_short(reply['reasoning'], 600)}")
        if reply["text"]:
            print(f"     [{turn}] says: {_short(reply['text'], 600)}")
        for call in reply["tool_calls"]:
            if call["name"] == "submit_plan":
                print(f"     [{turn}] submit_plan({_short(call['arguments'], 400)})")
                continue
            done = next(results, None)
            outcome = _short(json.dumps(done.result, default=str), 400) if done else "(not run)"
            print(f"     [{turn}] {call['name']}({call['arguments']}) -> {outcome}")
    print(f"     summary: {result.operator_summary}")


def save_run(scenario: str, run: int, agent: LocalAgent, result: AgentResult, problems: list[str]) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    model = re.sub(r"[^A-Za-z0-9.-]+", "_", agent.model)
    path = LOG_DIR / f"{model}-{scenario}-{run}.json"
    record = {"problems": problems, "transcript": agent.transcript, "result": result.model_dump(mode="json")}
    path.write_text(json.dumps(record, indent=2, default=str))
    return path


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=list(SLOTS), action="append")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the model's messages, reasoning and tool calls; save each run to eval-logs/")
    args = parser.parse_args()

    print(f"Model: {os.environ.get('VLLM_MODEL', DEFAULT_MODEL)} at {os.environ.get('VLLM_BASE_URL')}")
    passed = total = calls_total = thinking_total = 0
    seconds_total = 0.0
    for scenario in args.scenario or list(SLOTS):
        for run in range(1, args.repeat + 1):
            factory = FakeFactory(scenario)
            agent = LocalAgent(factory=factory.client(), manual_sections=MANUAL)
            start = time.time()
            result = runner.run(sample_alert(), agent=agent, backend="local")
            problems = check(scenario, result, factory)
            total += 1
            passed += not problems
            label = "PASS" if not problems else "FAIL"
            calls, seconds, thinking = speed(agent.transcript)
            calls_total, seconds_total, thinking_total = calls_total + calls, seconds_total + seconds, thinking_total + thinking
            print(f"{label} {scenario} #{run}: {result.status}, {len(result.trace)} tool calls, {time.time() - start:.0f}s"
                  f" | {calls} model calls, avg {seconds / max(calls, 1):.1f}s/call, {thinking} chars of thinking")
            for problem in problems:
                print(f"     - {problem}")
            if args.verbose:
                print_run(agent, result)
                print(f"     full log: {save_run(scenario, run, agent, result, problems)}")
    print(f"\n{passed}/{total} passed | {calls_total} model calls, avg {seconds_total / max(calls_total, 1):.1f}s/call, "
          f"{thinking_total} chars of thinking")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
