from __future__ import annotations

from datetime import timedelta

import pytest

from agent import manual, runner
from agent.local_agent import LocalAgent
from devtools.fakes import FakeFactory, ScriptedLLM
from devtools.scenarios import ENGINEERS, NOW, SCENARIOS
from scripts.eval_agent import check

REAL_MANUAL = manual.load_sections("manual")


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_expectations_follow_from_its_data(name):
    """The expected outcome is the only correct one: the expected slot is the earliest that fits the window,
    and an escalation means no slot fits."""
    s = SCENARIOS[name]
    if not s.search:
        assert s.booking is None
        return
    skill, minutes = s.search
    must_wait = s.order is not False and s.stock.get(s.part, 0) == 0
    start = s.delivery if must_wait else NOW
    fitting = sorted(
        (NOW + timedelta(hours=h), eid)
        for eid, hours in s.slots.items() if skill in ENGINEERS[eid][1]
        for h in hours
        if NOW + timedelta(hours=h) >= start and NOW + timedelta(hours=h, minutes=minutes) <= s.failure_at
    )
    if s.booking:
        engineer, hours = s.booking
        assert fitting and fitting[0] == (NOW + timedelta(hours=hours), engineer)
        assert (s.order is True) == must_wait
    else:
        assert not fitting


def run(name, steps):
    factory = FakeFactory(name)
    agent = LocalAgent(llm=ScriptedLLM(steps), factory=factory.client(), manual_sections=REAL_MANUAL, model="t")
    return runner.run(SCENARIOS[name].alert(), agent=agent, backend="local"), factory


def plan(**fields):
    base = {"status": "awaiting_approval", "diagnosis": "x", "repair_steps": ["step"], "part_number": None,
            "escalation_reason": None, "operator_summary": "Plain words for the operator."}
    return ("submit_plan", {**base, **fields})


def test_look_alike_finishing_tool_passes_when_done_right():
    result, factory = run("finishing_tool", [
        ("search_manual", {"query": "finishing tool"}),
        ("check_inventory", {"part_number": "EM-1006"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": NOW.isoformat(),
                                 "latest_end": SCENARIOS["finishing_tool"].failure_at.isoformat(), "duration_minutes": 25}),
        ("draft_booking", {"engineer_id": "E-01", "machine_id": "M-01", "start": "2026-10-14T15:00:00+00:00",
                           "end": "2026-10-14T15:25:00+00:00", "task": "Replace finishing tool"}),
        plan(part_number="EM-1006"),
    ])
    assert check("finishing_tool", result, factory) == []


def test_ordering_the_look_alike_is_caught():
    result, factory = run("finishing_tool_out", [
        ("search_manual", {"query": "tool wear end mill"}),
        ("check_inventory", {"part_number": "EM-1004"}),
        ("draft_purchase_order", {"part_number": "EM-1004", "quantity": 1, "machine_id": "M-01", "reason": "x"}),
        plan(status="escalated", part_number="EM-1004", escalation_reason="none"),
    ])
    problems = check("finishing_tool_out", result, factory)
    assert any("wrong part 'EM-1004'" in p for p in problems)
    assert any("ordered the wrong part: EM-1004" in p for p in problems)


def test_no_part_in_the_manual_escalates_without_inventing_one():
    good, factory = run("spindle_bearing", [
        ("search_manual", {"query": "spindle bearing noise vibration"}),
        plan(status="escalated", escalation_reason="The manual lists no bearing part; OEM diagnosis needed."),
    ])
    assert good.status == "escalated" and check("spindle_bearing", good, factory) == []

    invented, factory = run("spindle_bearing", [
        ("search_manual", {"query": "spindle bearing"}),
        ("draft_purchase_order", {"part_number": "SB-7000", "quantity": 1, "machine_id": "M-01", "reason": "x"}),
        plan(status="escalated", part_number="SB-7000", escalation_reason="x"),
    ])
    assert not factory.orders  # refused: the part is not in the manual
    assert any("named part 'SB-7000'" in p for p in check("spindle_bearing", invented, factory))


def test_wrong_skill_in_the_engineer_search_is_caught():
    s = SCENARIOS["coolant_pump"]
    result, factory = run("coolant_pump", [
        ("search_manual", {"query": "coolant pump flow"}),
        ("check_inventory", {"part_number": "CP-220"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": NOW.isoformat(),
                                 "latest_end": s.failure_at.isoformat(), "duration_minutes": 90}),
        plan(status="escalated", part_number="CP-220", escalation_reason="No engineer."),
    ])
    problems = check("coolant_pump", result, factory)
    assert any("skill 'cnc_tooling'" in p for p in problems)
    assert any("expected 1 booking" in p for p in problems)
