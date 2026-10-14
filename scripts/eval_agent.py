"""Run the real model (vLLM) through the three demo scenarios against fake factory systems.

Checks what the agent actually did in the factory systems, not only what it claims. Use it to pick the
model and tune the prompt before the mocks and Evolus are ready.

Usage: python -m scripts.eval_agent [--scenario NAME]... [--core] [--repeat N] [-v] [--short-manual]
Runs every scenario in devtools/scenarios.py: the three demo scenarios plus stress scenarios (look-alike parts,
other skills, parts that arrive too late, no part in the manual, ...); --core runs only the demo three.
Uses the real machine manual (manual/), as in the demo; --short-manual uses the 3-section test manual.
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

from agent import manual, runner
from agent.local_agent import DEFAULT_MODEL, LocalAgent
from agent.models import AgentResult
from agent.prompts import thinking_chars
from agent.times import utc
from devtools.fakes import MANUAL, FakeFactory
from devtools.scenarios import CORE, NOW, SCENARIOS, Scenario

LOG_DIR = Path("eval-logs")


def check(name: str, result: AgentResult, factory: FakeFactory) -> list[str]:
    """Return the problems found against the scenario's expectations (devtools/scenarios.py); empty = passed."""
    scenario = SCENARIOS[name]
    problems = [f"warning: {w}" for w in result.warnings]
    if result.status == "failed":
        return problems or ["agent failed"]
    orders, bookings = list(factory.orders.values()), list(factory.bookings.values())
    if scenario.part and result.part_number != scenario.part:
        problems.append(f"wrong part {result.part_number!r}, expected {scenario.part}")
    if not scenario.part and result.part_number:
        problems.append(f"named part {result.part_number!r}, but the manual lists none for {scenario.failure_type}")
    if not result.operator_summary:
        problems.append("no operator summary")
    wrong = [o["part_number"] for o in orders if o["part_number"] != scenario.part]
    if wrong:
        problems.append(f"ordered the wrong part: {', '.join(wrong)}")
    if len(orders) > 1 or len(bookings) > 1:
        problems.append(f"drafted {len(orders)} orders and {len(bookings)} bookings, expected at most 1 each")
    if scenario.order is True and len(orders) != 1:
        problems.append(f"expected 1 purchase order, got {len(orders)}")
    if scenario.order is False and orders:
        problems.append("ordered a part that is in stock" if scenario.part else "ordered a part the manual doesn't list")

    if scenario.booking:
        engineer, hours = scenario.booking
        expected = NOW + timedelta(hours=hours)
        if result.status != "awaiting_approval" or len(bookings) != 1:
            problems.append(f"expected 1 booking awaiting approval, got {len(bookings)} ({result.status})")
        elif utc(bookings[0]["start"]) != expected or bookings[0]["engineer_id"] != engineer:
            problems.append(f"booked {bookings[0]['engineer_id']} at {bookings[0]['start']}, "
                            f"expected {engineer} at {expected.isoformat()} (the earliest slot that fits)")
    else:
        if result.status != "escalated":
            problems.append(f"expected escalated, got {result.status}")
        if bookings:
            problems.append("booked an engineer although no slot fits")
    return problems + check_search_window(scenario, result, ordered=bool(orders))


def check_search_window(scenario: Scenario, result: AgentResult, ordered: bool) -> list[str]:
    """The engineer search must cover delivery (or now, if nothing was ordered) → predicted failure, with the
    manual's skill and repair time. A wrong window can still produce the right-looking outcome (no slots →
    escalate), so check it directly."""
    if scenario.search is None:
        return []
    searches = [c for c in result.trace if c.tool == "find_free_engineers"]
    if not searches:
        return ["never searched for an engineer"] if scenario.search_required else []
    args = searches[-1].arguments
    skill, minutes = scenario.search
    expected_start = scenario.delivery if ordered else NOW
    problems = []
    if utc(args.get("earliest_start")) != expected_start:
        problems.append(f"engineer search starts {args.get('earliest_start')}, expected {expected_start.isoformat()}")
    if utc(args.get("latest_end")) != scenario.failure_at:
        problems.append(f"engineer search ends {args.get('latest_end')}, expected {scenario.failure_at.isoformat()}")
    if args.get("duration_minutes") != minutes:
        problems.append(f"engineer search asks for {args.get('duration_minutes')} min, the manual says {minutes}")
    if args.get("skill") != skill:
        problems.append(f"engineer search asks for skill {args.get('skill')!r}, the manual says {skill!r}")
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
    parser.add_argument("--scenario", choices=list(SCENARIOS), action="append")
    parser.add_argument("--core", action="store_true", help="only the three demo scenarios")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the model's messages, reasoning and tool calls; save each run to eval-logs/")
    parser.add_argument("--short-manual", action="store_true", help="use the 3-section test manual (devtools.fakes)")
    args = parser.parse_args()
    sections = MANUAL if args.short_manual else manual.load_sections("manual")

    print(f"Model: {os.environ.get('VLLM_MODEL', DEFAULT_MODEL)} at {os.environ.get('VLLM_BASE_URL')}, "
          f"manual: {'short test manual' if args.short_manual else f'manual/ ({len(sections)} sections)'}")
    passed = total = calls_total = thinking_total = 0
    seconds_total = 0.0
    names = args.scenario or (CORE if args.core else list(SCENARIOS))
    by_scenario: dict[str, list[bool]] = {}
    for scenario in names:
        for run in range(1, args.repeat + 1):
            factory = FakeFactory(scenario)
            agent = LocalAgent(factory=factory.client(), manual_sections=sections)
            start = time.time()
            result = runner.run(SCENARIOS[scenario].alert(), agent=agent, backend="local")
            problems = check(scenario, result, factory)
            by_scenario.setdefault(scenario, []).append(not problems)
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
    print("\nBy scenario:")
    for name, runs in by_scenario.items():
        print(f"  {sum(runs)}/{len(runs)}  {name}: {SCENARIOS[name].about}")
    print(f"\n{passed}/{total} passed | {calls_total} model calls, avg {seconds_total / max(calls_total, 1):.1f}s/call, "
          f"{thinking_total} chars of thinking")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
