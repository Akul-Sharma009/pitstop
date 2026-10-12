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


def test_refused_booking_ends_as_escalation():
    result, factory = run(thin_path_script(), scenario="no_engineer")
    assert "BK-0001" not in factory.bookings  # the calendar refuses a slot that is not free
    assert result.status == "escalated" and result.escalation_reason
    # The plan still claimed BK-0001; the manager is told it doesn't exist.
    assert check("no_engineer", result, factory) == [
        "warning: The agent reported booking BK-0001, but no booking was drafted."
    ]


def test_eval_catches_wrong_part_ordered():
    result, factory = run(thin_path_script())
    factory.orders["PO-0002"] = {"part_number": "T-789"}
    assert any("ordered the wrong part: T-789" in p for p in check("out_of_stock", result, factory))


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


def test_in_stock_earliest_slot_accepts_z_suffix():
    script = thin_path_script()
    start, end = "2026-10-14T15:00:00Z", "2026-10-14T15:30:00Z"
    search = {**script[4][1], "earliest_start": "2026-10-14T08:00:00Z"}
    booking = {**script[5][1], "start": start, "end": end, "purchase_order_id": None}
    in_stock = [script[0], script[1], ("find_free_engineers", search), ("draft_booking", booking),
                ("submit_plan", {**script[6][1], "part_in_stock": True, "purchase_order_id": None})]
    result, factory = run(in_stock, scenario="in_stock")
    assert check("in_stock", result, factory) == []


def test_eval_catches_wrong_search_window_even_when_outcome_looks_right():
    script = thin_path_script()[:4] + [
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": "2026-10-16T08:00:00Z",
                                 "latest_end": "2026-10-16T08:00:00Z", "duration_minutes": 120}),
        ("submit_plan", {"status": "escalated", "diagnosis": "Worn tool.", "repair_steps": [], "part_number": PART,
                         "escalation_reason": None, "operator_summary": "x"}),
    ]
    result, factory = run(script, scenario="no_engineer")
    assert result.status == "escalated"
    problems = check("no_engineer", result, factory)
    assert any("search starts 2026-10-16T08:00:00Z" in p for p in problems)
    assert any("120 min" in p for p in problems)


def test_transcript_records_text_reasoning_and_calls():
    class ReasoningLLM(ScriptedLLM):
        def _create(self, **kwargs):
            response = super()._create(**kwargs)
            response.choices[0].message.reasoning = f"step {len(self.requests)}"
            return response

    factory = FakeFactory()
    agent = LocalAgent(llm=ReasoningLLM(["Let me check the manual.", *thin_path_script()]),
                       factory=factory.client(), manual_sections=MANUAL, model="test")
    runner.run(sample_alert(), agent=agent, backend="local")
    first, second = agent.transcript[0], agent.transcript[1]
    assert {k: first[k] for k in ("text", "reasoning", "tool_calls")} == {
        "text": "Let me check the manual.", "reasoning": "step 1", "tool_calls": []}
    assert second["tool_calls"][0]["name"] == "search_manual" and second["reasoning"] == "step 2"
    assert agent.transcript[-1]["tool_calls"][0]["name"] == "submit_plan"


def test_reasoning_found_in_think_block():
    from agent.prompts import reasoning_of

    assert reasoning_of(SimpleNamespace(content="<think>hmm</think>Answer")) == "hmm"


def test_thinking_probe_covers_each_switch():
    from scripts.check_vllm import probe_variants

    extras = [extra for _, _, extra in probe_variants()]
    assert {} in extras and {"think": False} in extras and {"reasoning_effort": "none"} in extras
    assert {"chat_template_kwargs": {"enable_thinking": False}} in extras
    assert any(message.endswith("/no_think") for _, message, _ in probe_variants())


def test_llm_extra_body_is_sent(monkeypatch):
    monkeypatch.setenv("LLM_EXTRA_BODY", '{"reasoning_effort": "none"}')
    llm = ScriptedLLM(thin_path_script())
    LocalAgent(llm=llm, factory=FakeFactory().client(), manual_sections=MANUAL, model="test").run(sample_alert())
    assert llm.requests[0]["extra_body"]["reasoning_effort"] == "none"


def test_probe_counts_long_text_before_a_tool_call_as_thinking():
    from scripts.check_vllm import describe_thinking

    call = [SimpleNamespace(function=SimpleNamespace(name="check_inventory", arguments="{}"))]
    rambling = "Okay, let's tackle this step by step. " * 10
    assert describe_thinking(SimpleNamespace(content=rambling, tool_calls=call)) == f"{len(rambling.strip())} chars (text)"
    assert describe_thinking(SimpleNamespace(content="Checking stock.", tool_calls=call)) == "none"
    assert describe_thinking(SimpleNamespace(content="", tool_calls=call, reasoning="hmm")) == "3 chars (field)"


def test_eval_speed_figures():
    from scripts.eval_agent import speed

    transcript = [
        {"seconds": 2.0, "text": "x" * 300, "reasoning": "", "tool_calls": [{"name": "search_manual"}]},
        {"seconds": 4.0, "text": "short", "reasoning": "abcd", "tool_calls": [{"name": "submit_plan"}]},
    ]
    assert speed(transcript) == (2, 6.0, 304)


def test_transcript_records_seconds_per_call():
    agent = LocalAgent(llm=ScriptedLLM(thin_path_script()), factory=FakeFactory().client(), manual_sections=MANUAL, model="t")
    agent.run(sample_alert())
    assert all(isinstance(reply["seconds"], float) for reply in agent.transcript)


def test_escalation_summary_keeps_part_numbers_and_full_sentences():
    from agent.reconcile import _escalation_summary

    base = {"part_number": PART, "purchase_order_id": None, "part_in_stock": True}
    assert _escalation_summary({**base, "diagnosis": "tool wear"}, "No engineer.").startswith("Problem found: tool wear. ")
    assert _escalation_summary({**base, "diagnosis": "EM-1004 is worn."}, "x").startswith("Problem found: EM-1004 is worn. ")
