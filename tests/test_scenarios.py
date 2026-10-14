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
        for eid, hours in s.slots.items() if skill in ENGINEERS[eid][1] and not (s.previous and eid in s.previous.avoid)
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


def test_replan_keeps_the_standing_order_and_books_someone_else():
    from scripts.eval_agent import prepare

    s = SCENARIOS["replan_engineer_on_leave"]
    factory = FakeFactory("replan_engineer_on_leave")
    replan = prepare("replan_engineer_on_leave", factory)
    assert replan.standing_order()["po_id"] == "PO-0001" and replan.booking["status"] == "rejected"
    delivery = s.delivery.isoformat()
    steps = [
        ("search_manual", {"query": "tool wear end mill"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": delivery,
                                 "latest_end": s.failure_at.isoformat(), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": "E-03", "machine_id": "M-01", "start": "2026-10-15T17:00:00+00:00",
                           "end": "2026-10-15T17:30:00+00:00", "task": "Replace end mill", "purchase_order_id": "PO-0001"}),
        plan(part_number="EM-1004", part_in_stock=False,
             operator_summary="Wei Ling Ho will replace the tool on Thursday at 17:00 UTC, once the manager approves."),
    ]
    llm = ScriptedLLM(steps)
    agent = LocalAgent(llm=llm, factory=factory.client(), manual_sections=REAL_MANUAL, model="t")
    result = runner.run(s.alert(), agent=agent, backend="local", replan=replan)
    assert "Priya Nair is on leave" in llm.requests[0]["messages"][1]["content"]
    assert result.trace[0].arguments == {"from_previous_plan": True}
    assert result.purchase_order_id == "PO-0001" and result.booking_id == "BK-0002"
    assert result.warnings == [] and check("replan_engineer_on_leave", result, factory) == []


def test_replan_cannot_order_the_part_again():
    from scripts.eval_agent import prepare

    factory = FakeFactory("replan_engineer_on_leave")
    replan = prepare("replan_engineer_on_leave", factory)
    result = runner.run(SCENARIOS["replan_engineer_on_leave"].alert(), backend="local", replan=replan, agent=LocalAgent(
        llm=ScriptedLLM([
            ("search_manual", {"query": "tool wear"}),
            ("draft_purchase_order", {"part_number": "EM-1004", "quantity": 1, "machine_id": "M-01", "reason": "x"}),
            plan(status="escalated", part_number="EM-1004", escalation_reason="x"),
        ]), factory=factory.client(), manual_sections=REAL_MANUAL, model="t"))
    refused = [c for c in result.trace if c.tool == "draft_purchase_order" and "error" in c.result]
    assert refused and "PO-0001 for EM-1004 is already on order" in refused[0].result["error"]
    assert list(factory.orders) == ["PO-0001"]
    assert any("expected 1 booking" in p for p in check("replan_engineer_on_leave", result, factory))


def test_no_booking_without_the_part():
    from scripts.eval_agent import prepare

    factory = FakeFactory("replan_budget_frozen")
    replan = prepare("replan_budget_frozen", factory)
    assert replan.standing_order() is None
    s = SCENARIOS["replan_budget_frozen"]
    result = runner.run(s.alert(), backend="local", replan=replan, agent=LocalAgent(
        llm=ScriptedLLM([
            ("search_manual", {"query": "tool wear"}),
            ("check_inventory", {"part_number": "EM-1004"}),
            ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": NOW.isoformat(),
                                     "latest_end": s.failure_at.isoformat(), "duration_minutes": 30}),
            plan(status="escalated", part_number="EM-1004", escalation_reason="Purchasing is frozen."),
        ]), factory=factory.client(), manual_sections=REAL_MANUAL, model="t"))
    search = [c for c in result.trace if c.tool == "find_free_engineers"][0]
    assert "not in stock and not on order" in search.result["error"]
    assert result.status == "escalated" and check("replan_budget_frozen", result, factory) == []


def fast_supplier_steps(supplier):
    s = SCENARIOS["fast_supplier"]
    delivery = (NOW + timedelta(hours=6 if supplier == "SpeedTool" else 24)).isoformat()
    return [
        ("search_manual", {"query": "tool wear end mill"}),
        ("check_inventory", {"part_number": "EM-1004"}),
        ("get_supplier_quote", {"part_number": "EM-1004"}),
        ("draft_purchase_order", {"part_number": "EM-1004", "quantity": 1, "machine_id": "M-01", "reason": "Tool wear",
                                  "supplier": supplier}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": delivery,
                                 "latest_end": s.failure_at.isoformat(), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": "E-01", "machine_id": "M-01", "start": "2026-10-14T15:00:00+00:00",
                           "end": "2026-10-14T15:30:00+00:00", "task": "Replace end mill", "purchase_order_id": "PO-0001"}),
        plan(part_number="EM-1004", part_in_stock=False, operator_summary="Priya Nair will replace the tool today at 15:00 UTC."),
    ]


def test_fast_supplier_when_the_cheap_one_is_too_late():
    result, factory = run("fast_supplier", fast_supplier_steps("SpeedTool"))
    assert factory.orders["PO-0001"]["supplier"] == "SpeedTool" and factory.orders["PO-0001"]["total_price"] == 145.0
    assert result.warnings == [] and check("fast_supplier", result, factory) == []


def test_cheap_supplier_too_late_is_caught():
    result, factory = run("fast_supplier", fast_supplier_steps("ToolCo"))
    problems = check("fast_supplier", result, factory)
    assert any("ordered from ToolCo, expected SpeedTool" in p for p in problems)
    assert not factory.bookings  # its delivery is after the failure: the calendar refused


def test_paying_for_speed_without_need_is_flagged():
    s = SCENARIOS["out_of_stock"]
    steps = [
        ("search_manual", {"query": "tool wear end mill"}),
        ("check_inventory", {"part_number": "EM-1004"}),
        ("get_supplier_quote", {"part_number": "EM-1004"}),
        ("draft_purchase_order", {"part_number": "EM-1004", "quantity": 1, "machine_id": "M-01", "reason": "x",
                                  "supplier": "SpeedTool"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": (NOW + timedelta(hours=6)).isoformat(),
                                 "latest_end": s.failure_at.isoformat(), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": "E-01", "machine_id": "M-01", "start": "2026-10-15T14:00:00+00:00",
                           "end": "2026-10-15T14:30:00+00:00", "task": "x", "purchase_order_id": "PO-0001"}),
        plan(part_number="EM-1004", part_in_stock=False),
    ]
    result, factory = run("out_of_stock", steps)
    assert any("ToolCo (EUR 89) would have arrived in time" in w for w in result.warnings)
    assert any("ordered from SpeedTool, expected ToolCo" in p for p in check("out_of_stock", result, factory))
