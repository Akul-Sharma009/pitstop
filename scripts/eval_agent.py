"""Run the real model (vLLM) through the three demo scenarios against fake factory systems.

Checks what the agent actually did in the factory systems, not only what it claims. Use it to pick the
model and tune the prompt before the mocks and Evolus are ready.

Usage: python -m scripts.eval_agent [--scenario NAME]... [--core] [--fleet] [--repeat N] [-v] [--short-manual]
Runs every scenario in devtools/scenarios.py: the three demo scenarios plus stress scenarios (look-alike parts,
other skills, parts that arrive too late, no part in the manual, ...) and replans after a manager's rejection;
--core runs only the demo three; --fleet runs fleet mode instead: four machines alert at once and four agents
plan in parallel on one shared factory (devtools.scenarios.FLEET).
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
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from agent import manual, runner
from agent.local_agent import DEFAULT_MODEL, LocalAgent
from agent.models import AgentResult, Replan
from agent.prompts import thinking_chars
from agent.times import utc
from devtools.fakes import MANUAL, FakeFactory
from devtools.scenarios import CORE, FLEET, NOW, PART, SCENARIOS, CHEAP, Scenario

LOG_DIR = Path("eval-logs")


def check(name: str, result: AgentResult, factory: FakeFactory) -> list[str]:
    """Return the problems found against the scenario's expectations (devtools/scenarios.py); empty = passed."""
    scenario = SCENARIOS[name]
    problems = [f"warning: {w}" for w in result.warnings]
    if result.status == "failed":
        return problems or ["agent failed"]
    # Drafts the manager rejected (in a replan scenario) don't count.
    orders = [o for o in factory.orders.values() if o.get("status") != "rejected"]
    bookings = [b for b in factory.bookings.values() if b.get("status") != "rejected"]
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
    if scenario.order is True and len(orders) == 1 and orders[0].get("supplier") != scenario.supplier:
        problems.append(f"ordered from {orders[0].get('supplier')}, expected {scenario.supplier} "
                        "(the cheapest supplier that delivers in time)")
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
    placed = next((o for o in reversed(orders) if o.get("part_number") == scenario.part and o.get("expected_delivery")), None)
    return problems + check_search_window(scenario, result, ordered=placed or bool(orders))


def prepare(name: str, factory: FakeFactory) -> Replan | None:
    """For a replan scenario, put the previous plan's drafts and the manager's decisions into the factory."""
    previous = SCENARIOS[name].previous
    if not previous:
        return None
    client, scenario = factory.client(), SCENARIOS[name]
    order = client.draft_purchase_order(scenario.part, 1, "M-01", "Tool wear")
    engineer, hours = previous.booking
    start = NOW + timedelta(hours=hours)
    booking = client.draft_booking(engineer, "M-01", start.isoformat(), (start + timedelta(minutes=30)).isoformat(),
                                   "Replace end mill", order["po_id"])
    decide = {"approved": "approve", "rejected": "reject"}
    for path, status in [(f"/supplier/purchase-orders/{order['po_id']}", previous.order_status),
                         (f"/calendar/bookings/{booking['booking_id']}", previous.booking_status)]:
        if status in decide:
            factory.handle(httpx.Request("POST", f"http://mocks{path}/{decide[status]}"))
    factory.calls.clear()
    return Replan(feedback=previous.feedback, purchase_order=factory.orders[order["po_id"]],
                  booking=factory.bookings[booking["booking_id"]])


def check_search_window(scenario: Scenario, result: AgentResult, ordered: dict | bool) -> list[str]:
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
    # The order the agent actually placed (e.g. the fast supplier when no offer is in time): search from its delivery.
    expected_start = (utc(ordered["expected_delivery"]) if isinstance(ordered, dict) else scenario.delivery) if ordered else NOW
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


def check_fleet(results: dict[str, AgentResult], factory: FakeFactory) -> list[str]:
    """Fleet mode: no slot or part promised twice, and each machine got what the shared factory allows."""
    problems = [f"{m} warning: {w}" for m, r in results.items() for w in r.warnings]
    problems += [f"{m} failed: {r.escalation_reason}" for m, r in results.items() if r.status == "failed"]
    bookings = [b for b in factory.bookings.values() if b["status"] != "rejected"]
    orders = [o for o in factory.orders.values() if o["status"] != "rejected"]
    slots = [(b["engineer_id"], utc(b["start"])) for b in bookings]
    if len(slots) != len(set(slots)):
        problems.append(f"a slot is booked twice: {sorted(slots)}")
    reserved = sum(1 for b in bookings if b.get("reserved_part") == PART)
    if reserved > FLEET.stock[PART]:
        problems.append(f"{reserved} bookings reserved {PART}, but only {FLEET.stock[PART]} is in stock")
    by_machine = {m: [b for b in bookings if b.get("machine_id") == m] for m in results}
    for machine in FLEET.escalates:
        if results[machine].status != "escalated" or by_machine[machine]:
            problems.append(f"{machine}: expected escalated with no booking, got {results[machine].status} "
                            f"and {len(by_machine[machine])} bookings")
    wear = [m.machine_id for m in FLEET.machines if m.machine_id not in FLEET.escalates]
    from_stock, ordered = [], []
    for machine in wear:
        mine = by_machine[machine]
        machine_orders = [o for o in orders if o.get("machine_id") == machine]
        if results[machine].status != "awaiting_approval" or len(mine) != 1:
            problems.append(f"{machine}: expected 1 booking awaiting approval, got {len(mine)} ({results[machine].status})")
            continue
        slot = (mine[0]["engineer_id"], (utc(mine[0]["start"]) - NOW).total_seconds() / 3600)
        if machine_orders:
            ordered.append(slot)
            if len(machine_orders) > 1 or machine_orders[0]["part_number"] != PART or machine_orders[0]["supplier"] != CHEAP:
                problems.append(f"{machine}: expected one {PART} order from {CHEAP}, got "
                                f"{[(o['part_number'], o['supplier']) for o in machine_orders]}")
        else:
            from_stock.append(slot)
    if from_stock != [FLEET.from_stock]:
        problems.append(f"expected one machine to use the stock part at {FLEET.from_stock}, got {from_stock}")
    if sorted(ordered) != sorted(FLEET.after_delivery):
        problems.append(f"expected the ordering machines at {FLEET.after_delivery}, got {sorted(ordered)}")
    return problems


def run_fleet(sections: list, repeat: int, verbose: bool) -> tuple[int, int]:
    """Four agents in parallel threads on one fake factory: (passed, total) runs."""
    passed = 0
    for run in range(1, repeat + 1):
        factory = FakeFactory("fleet")
        agents = {m.machine_id: LocalAgent(factory=factory.client(), manual_sections=sections) for m in FLEET.machines}
        start = time.time()
        with ThreadPoolExecutor(len(FLEET.machines)) as pool:
            futures = {m.machine_id: pool.submit(runner.run, m.alert(), agents[m.machine_id], "local")
                       for m in FLEET.machines}
            results = {machine: future.result() for machine, future in futures.items()}
        problems = check_fleet(results, factory)
        passed += not problems
        calls = sum(len(a.transcript) for a in agents.values())
        print(f"{'PASS' if not problems else 'FAIL'} fleet #{run}: {time.time() - start:.0f}s, {calls} model calls")
        for machine, result in results.items():
            booking = f"{result.engineer_name} {result.booking_start:%a %H:%M}" if result.booking_start else "no booking"
            order = f"{result.purchase_order_id}" if result.purchase_order_id else "no order"
            print(f"     {machine} {result.status}: {result.part_number}, {order}, {booking}")
        for problem in problems:
            print(f"     - {problem}")
        if verbose:
            for machine, agent in agents.items():
                print(f"   {machine}:")
                print_run(agent, results[machine])
                print(f"     full log: {save_run(f'fleet-{machine}', run, agent, results[machine], problems)}")
    print(f"\n{passed}/{repeat} fleet runs passed: {FLEET.about}")
    return passed, repeat


def speed(transcript: list[dict[str, Any]]) -> tuple[int, float, int]:
    """(model calls, total seconds in the model, chars of thinking) for one run."""
    seconds = sum(reply.get("seconds", 0) for reply in transcript)
    thinking = sum(
        sum(thinking_chars(reply["reasoning"], reply["text"], bool(reply["tool_calls"]))) for reply in transcript
    )
    return len(transcript), seconds, thinking


def print_run(agent: LocalAgent, result: AgentResult) -> None:
    """The model's text and reasoning for each reply, followed by the tool results it got back."""
    results = iter([c for c in result.trace if not c.arguments.get("from_previous_plan")])  # not a call of this run
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
    parser.add_argument("--fleet", action="store_true", help="fleet mode: four machines plan in parallel")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the model's messages, reasoning and tool calls; save each run to eval-logs/")
    parser.add_argument("--short-manual", action="store_true", help="use the 3-section test manual (devtools.fakes)")
    args = parser.parse_args()
    sections = MANUAL if args.short_manual else manual.load_sections("manual")

    print(f"Model: {os.environ.get('VLLM_MODEL', DEFAULT_MODEL)} at {os.environ.get('VLLM_BASE_URL')}, "
          f"manual: {'short test manual' if args.short_manual else f'manual/ ({len(sections)} sections)'}")
    if args.fleet:
        passed, total = run_fleet(sections, args.repeat, args.verbose)
        return 0 if passed == total else 1
    passed = total = calls_total = thinking_total = 0
    seconds_total = 0.0
    names = args.scenario or (CORE if args.core else list(SCENARIOS))
    by_scenario: dict[str, list[bool]] = {}
    for scenario in names:
        for run in range(1, args.repeat + 1):
            factory = FakeFactory(scenario)
            agent = LocalAgent(factory=factory.client(), manual_sections=sections)
            start = time.time()
            replan = prepare(scenario, factory)
            result = runner.run(SCENARIOS[scenario].alert(), agent=agent, backend="local", replan=replan)
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
