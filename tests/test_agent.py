from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent import api, manual, runner
from agent.evolus_client import EvolusAgent, extract_json
from agent.local_agent import LocalAgent
from devtools.fakes import FAILURE, MANUAL, PART, FakeFactory, ScriptedLLM, sample_alert, thin_path_script


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
    # The local agent refuses it, so nothing is booked and the plan escalates.
    assert result.booking_id is None and result.status == "escalated"
    assert "after the predicted failure" in result.trace[-1].result["error"]
    # Evolus has no such refusal: there the guard flags a late booking.
    from agent import guard
    late_plan = result.model_copy(update={"status": "awaiting_approval", "booking_id": "BK-0001",
                                          "booking_start": datetime.fromisoformat(late["start"]),
                                          "booking_end": datetime.fromisoformat(late["end"])})
    assert "Repair is booked after the predicted failure." in guard.check(late_plan, sample_alert())


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
    agent = EvolusAgent(llm=ScriptedLLM([reply]), agent_id="app:agent-1", factory_log=factory_log(FakeFactory()))
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
    agent = EvolusAgent(llm=ScriptedLLM([reply]), agent_id="a", factory_log=factory_log(FakeFactory()))
    result = runner.run(make_alert(), agent=agent, backend="evolus")
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


def factory_log(factory):
    import httpx

    from agent.factory_log import FactoryLog

    return FactoryLog(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))


def test_evolus_run_is_traced_and_checked_from_the_factory_log():
    """Evolus calls our tools itself and replies with text: the trace comes from the mock factory."""
    factory = FakeFactory()
    factory.client().check_inventory(PART)  # an earlier run's call must not leak into this trace
    script = thin_path_script()
    plan = {k: v for k, v in script[-1][1].items() if k not in {"purchase_order_id", "booking_id", "expected_delivery"}}

    class EvolusLike(ScriptedLLM):
        def _create(self, **kwargs):
            self.requests.append(kwargs)
            client = factory.client()
            for name, arguments in script[1:-1]:  # the knowledge library search happens inside Evolus
                getattr(client, name)(**arguments)
            message = SimpleNamespace(content="Done.\n" + json.dumps(plan), tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    llm = EvolusLike([])
    agent = EvolusAgent(llm=llm, agent_id="app:agent", factory_log=factory_log(factory))
    result = runner.run(sample_alert(), agent=agent, backend="evolus")
    assert llm.requests[0]["model"] == "app:agent"
    assert [c.tool for c in result.trace] == [name for name, _ in script[1:-1]]
    assert result.purchase_order_id == "PO-0001" and result.booking_id == "BK-0001"
    assert result.status == "awaiting_approval" and result.warnings == []


def test_evolus_plan_wrapped_in_a_schema_outline_is_unwrapped():
    plan = {"status": "awaiting_approval", "diagnosis": "worn", "repair_steps": [], "part_number": PART,
            "escalation_reason": None, "operator_summary": "x"}
    reply = json.dumps({"type": "object", "properties": plan, "required": ["status"]})
    assert extract_json(reply) == plan
    assert extract_json("Plan:\n" + json.dumps({**plan, "repair_steps": [{"step": 1}]}))["status"] == "awaiting_approval"


def test_evolus_message_asks_for_english_and_an_example_not_a_schema():
    from agent.evolus_client import evolus_message
    from agent.prompts import EVOLUS_SYSTEM_PROMPT

    message = evolus_message(make_alert())
    assert "in English" in message and '"properties"' not in message and '"operator_summary"' in message
    assert "Always write in English" in EVOLUS_SYSTEM_PROMPT


def test_downtime_saved_from_the_booking_and_stated_assumptions(monkeypatch):
    result, _ = run_local(thin_path_script())
    assert result.downtime.planned_min == 30 and result.downtime.unplanned_h == 8
    assert result.downtime.saved_h == 7.5 and result.downtime.money_saved == 3750 and result.downtime.currency == "SGD"
    monkeypatch.setenv("UNPLANNED_DOWNTIME_H", "6")
    monkeypatch.setenv("DOWNTIME_COST_PER_H", "1000")
    result, _ = run_local(thin_path_script())
    assert result.downtime.money_saved == 5500 and "about 6 h" in result.downtime.assumptions


def test_no_downtime_figure_without_a_booking():
    result, _ = run_local(thin_path_script(), scenario="no_engineer")
    assert result.status == "escalated" and result.downtime is None


def test_latest_incident_is_kept_for_the_screens(monkeypatch):
    monkeypatch.setattr(api, "incidents", {})
    monkeypatch.setattr(runner, "make_agent", lambda backend=None: LocalAgent(
        llm=ScriptedLLM(thin_path_script()), factory=FakeFactory().client(), manual_sections=MANUAL, model="test"
    ))
    client = TestClient(api.app)
    assert client.get("/incidents/latest").status_code == 404
    client.post("/incidents", json=make_alert().model_dump(mode="json"))
    latest = client.get("/incidents/latest").json()
    assert latest["alert"]["machine_id"] == "M-01" and latest["result"]["booking_id"] == "BK-0001"
    assert latest["result"]["downtime"]["saved_h"] == 7.5


def test_replan_endpoint_plans_again_with_the_feedback(monkeypatch):
    from agent.prompts import replan_message
    from agent.models import Replan

    monkeypatch.setattr(api, "incidents", {})
    llms = []

    def make(backend=None):
        llms.append(ScriptedLLM(thin_path_script()))
        return LocalAgent(llm=llms[-1], factory=FakeFactory().client(), manual_sections=MANUAL, model="test")

    monkeypatch.setattr(runner, "make_agent", make)
    client = TestClient(api.app)
    assert client.post("/incidents/replan", json={"feedback": "x"}).status_code == 404
    first = client.post("/incidents", json=make_alert().model_dump(mode="json")).json()
    order = {"po_id": "PO-0001", "part_number": PART, "expected_delivery": "2026-10-15T08:00:00+00:00", "status": "approved"}
    booking = {"booking_id": "BK-0001", "engineer_id": "E-01", "start": "2026-10-15T14:00:00+00:00", "status": "rejected"}
    response = client.post("/incidents/replan", json={"feedback": "Priya is on leave", "purchase_order": order, "booking": booking})
    assert response.status_code == 200
    latest = client.get("/incidents/latest").json()
    assert latest["feedback"] == "Priya is on leave" and latest["previous"]["booking_id"] == first["booking_id"]
    sent = llms[-1].requests[0]["messages"][1]["content"]
    assert 'feedback: "Priya is on leave"' in sent and "approved by the manager" in sent and "REJECTED" in sent
    standing = replan_message(Replan(feedback="x", purchase_order=order))
    assert "Do NOT call get_supplier_quote or draft_purchase_order" in standing
    assert "earliest_start 2026-10-15T08:00:00+00:00" in standing and "purchase_order_id PO-0001" in standing
    rejected = replan_message(Replan(feedback="x", purchase_order={**order, "status": "rejected"}))
    assert "do not order EM-1004 again" in rejected and "no repair can be booked without it" in rejected
    assert "Do NOT call" not in rejected


def test_guard_flags_two_orders_and_a_booking_without_the_part():
    from agent import guard
    from agent.models import AgentResult, ToolCall

    order = lambda po: ToolCall(tool="draft_purchase_order", arguments={}, result={"po_id": po, "part_number": PART, "status": "pending_approval"})
    booking = ToolCall(tool="draft_booking", arguments={}, result={"booking_id": "BK-0001"})
    two = AgentResult(status="awaiting_approval", machine_id="M-01", part_number=PART, booking_id="BK-0001",
                      trace=[order("PO-0001"), order("PO-0002"), booking])
    assert any("2 orders were drafted for EM-1004: PO-0001, PO-0002" in w for w in guard.check(two, make_alert()))
    stock = ToolCall(tool="check_inventory", arguments={}, result={"part_number": PART, "quantity_on_hand": 0})
    none = AgentResult(status="awaiting_approval", machine_id="M-01", part_number=PART, booking_id="BK-0001",
                       trace=[stock, booking])
    assert any("neither in stock nor on order" in w for w in guard.check(none, make_alert()))


def test_a_search_or_booking_past_the_predicted_failure_is_refused():
    agent = LocalAgent(llm=ScriptedLLM([]), factory=FakeFactory().client(), manual_sections=MANUAL, model="test")
    agent.failure_at = FAILURE
    later = (FAILURE + timedelta(days=1)).isoformat()
    refused = agent._after_failure("find_free_engineers", {"latest_end": later})
    assert refused and FAILURE.isoformat() in refused and "escalate" in refused
    assert agent._after_failure("draft_booking", {"end": later})
    assert agent._after_failure("find_free_engineers", {"latest_end": FAILURE.isoformat()}) is None


def test_guard_flags_an_order_for_more_than_one_unit():
    from agent import guard
    script = thin_path_script()
    script[3] = ("draft_purchase_order", {**script[3][1], "quantity": 50})
    result, _ = run_local(script)
    assert any("orders 50 units of EM-1004; a repair needs 1" in w for w in result.warnings)
