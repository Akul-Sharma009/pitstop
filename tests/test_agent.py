from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import api, manual, runner
from agent.evolus_client import EvolusAgent, extract_json
from agent.local_agent import LocalAgent
from devtools.fakes import MANUAL, PART, FakeFactory, ScriptedLLM, sample_alert, thin_path_script


make_alert = sample_alert


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
    # The calendar has a free slot on 16 Oct 12:00 in this scenario, after the predicted failure (08:00).
    script = thin_path_script()
    late = {"start": "2026-10-16T12:00:00+00:00", "end": "2026-10-16T12:30:00+00:00"}
    script[5] = ("draft_booking", {**script[5][1], **late})
    result, _ = run_local(script, scenario="no_engineer")
    assert result.booking_end.isoformat() == late["end"]  # taken from the calendar, not the model
    assert "Repair is booked after the predicted failure." in result.warnings


def test_model_that_never_submits_becomes_failed_result():
    result, _ = run_local(["I think the tool is worn.", "Still thinking."])
    assert result.status == "failed"
    assert result.warnings


def test_bad_plan_becomes_failed_result():
    script = thin_path_script()
    plan = dict(script[-1][1], repair_steps=5)
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


def test_no_think_switch_and_thinking_stripped(monkeypatch):
    monkeypatch.setenv("LLM_NO_THINK", "true")
    llm = ScriptedLLM(["<think>Maybe order it?</think>Let me check.", *thin_path_script()])
    agent = LocalAgent(llm=llm, factory=FakeFactory().client(), manual_sections=MANUAL, model="test")
    result = runner.run(make_alert(), agent=agent, backend="local")

    assert result.status == "awaiting_approval"
    first_request = llm.requests[0]["messages"]
    assert first_request[1]["content"].endswith("/no_think")
    replies = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "assistant"]
    assert "Let me check." in replies and not any("<think>" in r for r in replies)


def test_evolus_reply_ignores_json_inside_thinking():
    reply = '<think>{"status": "awaiting_approval"}</think>{"status": "escalated", "diagnosis": "worn", ' \
            '"repair_steps": [], "part_number": "EM-1004", "operator_summary": "x", "escalation_reason": "none free"}'
    result = runner.run(make_alert(), agent=EvolusAgent(llm=ScriptedLLM([reply]), agent_id="a"), backend="evolus")
    assert result.status == "escalated"


def test_ids_and_times_come_from_the_tools_when_the_plan_leaves_them_out():
    script = thin_path_script()
    keep = {"status", "diagnosis", "repair_steps", "part_number", "part_name", "operator_summary"}
    plan = {k: v for k, v in script[-1][1].items() if k in keep}
    result, _ = run_local([*script[:-1], ("submit_plan", plan)])

    assert result.warnings == []
    assert (result.purchase_order_id, result.booking_id, result.engineer_name) == ("PO-0001", "BK-0001", "Priya Nair")
    assert result.booking_start.isoformat() == "2026-10-15T14:00:00+00:00"
    assert result.part_in_stock is False


def test_conflicting_claim_is_corrected_and_flagged():
    script = thin_path_script()
    plan = dict(script[-1][1], booking_id="BK-9999")
    result, _ = run_local([*script[:-1], ("submit_plan", plan)])
    assert result.booking_id == "BK-0001"
    assert any("BK-9999" in w for w in result.warnings)


def test_escalation_reason_filled_when_no_engineer_is_free():
    script = [
        *thin_path_script()[:4],
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": "2026-10-15T08:00:00+00:00",
                                 "latest_end": "2026-10-16T08:00:00+00:00", "duration_minutes": 30}),
        ("submit_plan", {"status": "escalated", "diagnosis": "Worn tool.", "repair_steps": [], "part_number": PART,
                         "operator_summary": "A manager will schedule the repair."}),
    ]
    result, factory = run_local(script, scenario="no_engineer")
    assert result.status == "escalated" and not factory.bookings
    assert result.escalation_reason == "No qualified engineer is free between Thu 15 Oct 08:00 UTC and Fri 16 Oct 08:00 UTC."
    assert result.warnings == []
    # The operator hears the facts, not a promise that someone is coming.
    assert result.operator_summary == (
        "Problem found: worn tool. A replacement part (EM-1004) has been ordered and should arrive Thu 15 Oct 08:00 UTC; "
        "the order is waiting for your manager's approval. No repair is booked yet: no qualified engineer is free "
        "between Thu 15 Oct 08:00 UTC and Fri 16 Oct 08:00 UTC. Your manager has been asked to arrange the repair."
    )


def test_invented_part_cannot_be_ordered():
    script = thin_path_script()
    script[3] = ("draft_purchase_order", {**script[3][1], "part_number": "T-789"})
    result, factory = run_local(script)
    refused = [c for c in result.trace if c.tool == "draft_purchase_order"][0]
    assert "not in the machine manual" in refused.result["error"]
    assert not factory.orders


def test_supplier_rejects_unknown_parts():
    result = FakeFactory().client().draft_purchase_order("T-789", 1, "M-01", "wear")
    assert result["error"].startswith("404")


def test_no_think_also_sets_system_prompt_and_ollama_switch(monkeypatch):
    monkeypatch.setenv("LLM_NO_THINK", "true")
    llm = ScriptedLLM(thin_path_script())
    LocalAgent(llm=llm, factory=FakeFactory().client(), manual_sections=MANUAL, model="test").run(make_alert())
    assert llm.requests[0]["messages"][0]["content"].endswith("/no_think")
    assert llm.requests[0]["extra_body"]["think"] is False


def test_repo_manual_grounds_the_tool_wear_part():
    from agent import manual

    sections = manual.load_sections("manual")
    hits = manual.search("tool wear end mill replacement", sections)
    assert hits and PART in manual.part_numbers(hits[0]["text"])


def test_empty_manual_is_reported_not_silent():
    agent = LocalAgent(llm=ScriptedLLM([]), factory=FakeFactory().client(), manual_sections=[], model="test")
    assert "not loaded" in agent._execute("search_manual", {"query": "tool wear"})["error"]


def test_health_reports_manual_sections(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from agent.api import app

    monkeypatch.setenv("MANUAL_DIR", str(tmp_path))
    assert TestClient(app).get("/health").json()["status"] == "no manual"
    monkeypatch.setenv("MANUAL_DIR", "manual")
    assert TestClient(app).get("/health").json()["manual_sections"] >= 3


def test_escalation_reason_comes_from_the_empty_search():
    from agent.models import ToolCall
    from agent.reconcile import reconcile

    search = ToolCall(tool="find_free_engineers", result={"slots": []},
                      arguments={"earliest_start": "2026-10-15T08:00:00Z", "latest_end": "2026-10-16T08:00:00Z"})
    plan, _ = reconcile({"escalation_reason": "after delivery on tomorrow (Thu 15 Oct) 08:00 UTC"}, [search])
    assert plan["escalation_reason"] == "No qualified engineer is free between Thu 15 Oct 08:00 UTC and Fri 16 Oct 08:00 UTC."
