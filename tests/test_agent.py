from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import api, manual, runner
from agent.evolus_client import EvolusAgent, extract_json
from agent.local_agent import LocalAgent
from agent.models import Alert
from tests.fakes import FAILURE, MANUAL, NOW, PART, FakeFactory, ScriptedLLM, thin_path_script


def make_alert() -> Alert:
    return Alert(
        machine_id="M-01",
        failure_type="tool_wear",
        risk=0.93,
        tool_wear_min=172,
        remaining_cutting_min=38,
        detected_at=NOW,
        predicted_failure_at=FAILURE,
    )


def run_local(script, scenario="out_of_stock"):
    factory = FakeFactory(scenario)
    agent = LocalAgent(llm=ScriptedLLM(script), factory=factory.client(), manual_sections=MANUAL, model="test")
    return runner.run(make_alert(), agent=agent, backend="local"), factory


def test_thin_path_drafts_order_and_booking_for_approval():
    result, factory = run_local(thin_path_script())

    assert result.status == "awaiting_approval"
    assert result.part_number == PART
    assert result.purchase_order_id == "PO-0001" and result.booking_id == "BK-0001"
    assert result.warnings == []
    assert [call.tool for call in result.trace] == [
        "search_manual", "check_inventory", "get_supplier_quote",
        "draft_purchase_order", "find_free_engineers", "draft_booking",
    ]
    # Drafts exist in the factory systems, but nothing is approved.
    assert factory.orders["PO-0001"]["status"] == "pending_approval"
    assert factory.bookings["BK-0001"]["status"] == "pending_approval"


def test_invented_part_number_is_flagged():
    script = thin_path_script()
    plan = dict(script[-1][1], part_number="XX-9999")
    result, _ = run_local([*script[:-1], ("submit_plan", plan)])
    assert any("XX-9999" in w for w in result.warnings)


def test_booking_after_failure_is_flagged():
    script = thin_path_script()
    plan = dict(script[-1][1], booking_end="2026-10-17T08:00:00+00:00")
    result, _ = run_local([*script[:-1], ("submit_plan", plan)])
    assert "Repair is booked after the predicted failure." in result.warnings


def test_model_that_never_submits_becomes_failed_result():
    result, _ = run_local(["I think the tool is worn.", "Still thinking."])
    assert result.status == "failed"
    assert result.warnings


def test_bad_plan_becomes_failed_result():
    script = thin_path_script()
    plan = dict(script[-1][1], booking_start="not a date")
    result, _ = run_local([*script[:-1], ("submit_plan", plan)])
    assert result.status == "failed"


def test_manual_search_finds_tool_wear_section():
    hits = manual.search("tool wear end mill", MANUAL)
    assert hits[0]["heading"] == "Tool wear"


def test_manual_loads_sections_from_markdown(tmp_path):
    (tmp_path / "m.md").write_text("# Machine\nIntro\n## Tool wear\nReplace EM-1004.\n")
    sections = manual.load_sections(tmp_path)
    assert [s.heading for s in sections] == ["Machine", "Tool wear"]


def test_evolus_backend_parses_json_reply():
    reply = 'Here is the plan:\n```json\n{"status": "escalated", "diagnosis": "worn", "repair_steps": [], ' \
            '"part_number": "EM-1004", "operator_summary": "x", "escalation_reason": "no engineer"}\n```'
    agent = EvolusAgent(llm=ScriptedLLM([reply]), agent_id="agent-1")
    result = runner.run(make_alert(), agent=agent, backend="evolus")
    assert result.status == "escalated"
    assert result.backend == "evolus"
    assert any("No tool trace" in w for w in result.warnings)


def test_extract_json_rejects_prose():
    with pytest.raises(Exception):
        extract_json("no plan here")


def test_api_returns_agent_result(monkeypatch):
    monkeypatch.setattr(runner, "make_agent", lambda backend=None: LocalAgent(
        llm=ScriptedLLM(thin_path_script()), factory=FakeFactory().client(), manual_sections=MANUAL, model="test"
    ))
    client = TestClient(api.app)
    response = client.post("/incidents", json=make_alert().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"
