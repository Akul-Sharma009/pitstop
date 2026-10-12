"""Run the real model (vLLM) through the three demo scenarios against fake factory systems.

Checks what the agent actually did in the factory systems, not only what it claims. Use it to pick the
model and tune the prompt before the mocks and Evolus are ready.

Usage: python -m scripts.eval_agent [--scenario out_of_stock|in_stock|no_engineer] [--repeat N] [-v]
Reads VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from agent import runner
from agent.local_agent import LocalAgent
from agent.models import AgentResult
from devtools.fakes import MANUAL, PART, SLOTS, FakeFactory, sample_alert

DELIVERY = datetime.fromisoformat("2026-10-15T08:00:00+00:00")  # sample alert time + 24 h supplier lead time


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

    if scenario == "out_of_stock":
        if len(factory.orders) != 1:
            problems.append(f"expected 1 purchase order, got {len(factory.orders)}")
        if result.status != "awaiting_approval" or len(bookings) != 1:
            problems.append(f"expected 1 booking awaiting approval, got {len(bookings)} ({result.status})")
        elif datetime.fromisoformat(bookings[0]["start"]) < DELIVERY:
            problems.append(f"booked at {bookings[0]['start']}, before the part arrives")
    elif scenario == "in_stock":
        if factory.orders:
            problems.append("ordered a part that is in stock")
        if result.status != "awaiting_approval" or len(bookings) != 1:
            problems.append(f"expected 1 booking awaiting approval, got {len(bookings)} ({result.status})")
        elif bookings[0]["start"] != SLOTS["in_stock"][0]:
            problems.append(f"booked {bookings[0]['start']}, not the earliest slot {SLOTS['in_stock'][0]}")
    elif scenario == "no_engineer":
        if result.status != "escalated":
            problems.append(f"expected escalated, got {result.status}")
        if bookings:
            problems.append("booked an engineer although no slot fits")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=list(SLOTS), action="append")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true", help="print the trace and summary")
    args = parser.parse_args()

    passed = total = 0
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
            print(f"{label} {scenario} #{run}: {result.status}, {len(result.trace)} tool calls, {time.time() - start:.0f}s")
            for problem in problems:
                print(f"     - {problem}")
            if args.verbose:
                for call in result.trace:
                    print(f"     {call.tool}({call.arguments}) -> {str(call.result)[:160]}")
                print(f"     summary: {result.operator_summary}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
