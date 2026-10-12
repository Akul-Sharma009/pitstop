from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent import runner
from agent.local_agent import LocalAgent
from devtools.fakes import MANUAL, PART, FakeFactory, ScriptedLLM, sample_alert, thin_path_script
from scripts import dev_mocks
from scripts.eval_agent import check


def run(script, scenario="out_of_stock"):
    factory = FakeFactory(scenario)
    agent = LocalAgent(llm=ScriptedLLM(script), factory=factory.client(), manual_sections=MANUAL, model="test")
    return runner.run(sample_alert(), agent=agent, backend="local"), factory


def test_eval_passes_good_thin_path():
    result, factory = run(thin_path_script())
    assert check("out_of_stock", result, factory) == []


def test_eval_catches_booking_before_delivery():
    script = thin_path_script()
    early = {"start": "2026-10-14T15:00:00+00:00", "end": "2026-10-14T15:30:00+00:00"}
    script[5] = ("draft_booking", {**script[5][1], **early})
    script[6] = ("submit_plan", {**script[6][1], "booking_start": early["start"], "booking_end": early["end"]})
    result, factory = run(script)
    problems = check("out_of_stock", result, factory)
    assert any("before the part arrives" in p for p in problems)


def test_eval_catches_missing_escalation():
    result, factory = run(thin_path_script(), scenario="no_engineer")
    assert "BK-0001" not in factory.bookings  # the calendar refuses a slot that is not free
    assert "expected escalated, got awaiting_approval" in check("no_engineer", result, factory)


def test_free_slots_respect_the_window():
    client = FakeFactory("out_of_stock").client()
    slots = client.find_free_engineers("cnc_tooling", "2026-10-15T08:00:00", "2026-10-16T08:00:00Z", 30)["slots"]
    assert [s["slot_start"] for s in slots] == ["2026-10-15T14:00:00+00:00"]


def test_submit_plan_in_same_turn_still_runs_other_calls():
    script = thin_path_script()
    booking, plan = script[5], script[6]

    class ParallelLLM(ScriptedLLM):
        def _create(self, **kwargs):
            if len(self.steps) == 2:  # emit draft_booking and submit_plan in one turn
                self.requests.append(kwargs)
                self.steps.clear()
                calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                         for i, (n, a) in enumerate([booking, plan])]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=calls))])
            return super()._create(**kwargs)

    factory = FakeFactory()
    agent = LocalAgent(llm=ParallelLLM(script), factory=factory.client(), manual_sections=MANUAL, model="test")
    result = runner.run(sample_alert(), agent=agent, backend="local")
    assert result.booking_id == "BK-0001" and "BK-0001" in factory.bookings
    assert result.warnings == []


def test_dev_mocks_serves_contract_and_resets():
    client = TestClient(dev_mocks.app)
    assert client.get(f"/inventory/parts/{PART}").json()["quantity_on_hand"] == 0
    assert client.post("/admin/reset", params={"scenario": "in_stock"}).json() == {"scenario": "in_stock"}
    assert client.get(f"/inventory/parts/{PART}").json()["quantity_on_hand"] == 2
    assert client.post("/admin/reset", params={"scenario": "nope"}).status_code == 400
    client.post("/admin/reset", params={"scenario": "out_of_stock"})
