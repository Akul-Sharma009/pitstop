"""Fleet mode: several machines share one factory (the same engineers and parts store)."""

from __future__ import annotations

from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from agent import api, runner
from agent.local_agent import LocalAgent
from devtools.fakes import MANUAL, PART, FakeFactory, ScriptedLLM
from devtools.scenarios import FLEET, NOW
from scripts.eval_agent import check_fleet


def at(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).isoformat()


def machine(factory: FakeFactory, machine_id: str):
    client = factory.client()
    client.machine_id = machine_id
    return client


def book(client, engineer: str, hours: float, **extra):
    return client.draft_booking(engineer, client.machine_id, at(hours), at(hours + 0.5), "repair", **extra)


def admin(factory: FakeFactory) -> httpx.Client:
    return httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))


def test_a_drafted_booking_holds_its_slot_until_rejected():
    factory = FakeFactory("fleet")
    m1, m2 = machine(factory, "M-01"), machine(factory, "M-02")
    first = book(m1, "E-01", 26, part_number=PART)
    taken = book(m2, "E-01", 26)
    assert taken["error"].startswith("409") and first["booking_id"] in taken["error"] and "M-01" in taken["error"]
    free = m2.find_free_engineers("cnc_tooling", at(0), at(48), 30)["slots"]
    assert at(26) not in [s["slot_start"] for s in free if s["engineer_id"] == "E-01"]
    admin(factory).post(f"/calendar/bookings/{first['booking_id']}/reject")
    assert "error" not in book(m2, "E-01", 26)  # given back


def test_a_booking_from_stock_reserves_the_part():
    factory = FakeFactory("fleet")
    m1, m2 = machine(factory, "M-01"), machine(factory, "M-02")
    assert m1.check_inventory(PART)["quantity_on_hand"] == 1
    first = book(m1, "E-01", 7, part_number=PART)
    assert first["reserved_part"] == PART
    assert m2.check_inventory(PART)["quantity_on_hand"] == 0  # reserved for M-01
    refused = book(m2, "E-01", 26, part_number=PART)
    assert refused["error"].startswith("409") and "reserved for BK-0001 (M-01)" in refused["error"]
    admin(factory).post(f"/calendar/bookings/{first['booking_id']}/reject")
    assert m2.check_inventory(PART)["quantity_on_hand"] == 1


def test_the_part_last_found_in_stock_is_reserved_without_part_number():
    """Evolus may leave part_number out: the factory reserves what this machine last found in stock."""
    factory = FakeFactory("fleet")
    m1 = machine(factory, "M-01")
    m1.check_inventory(PART)
    assert book(m1, "E-01", 7)["reserved_part"] == PART
    m2 = machine(factory, "M-02")
    m2.check_inventory(PART)  # 0 left: nothing to reserve, so no reservation either
    assert "reserved_part" not in book(m2, "E-03", 27)


def test_the_delivery_rule_only_counts_the_machines_own_orders():
    factory = FakeFactory("fleet")
    m1, m2 = machine(factory, "M-01"), machine(factory, "M-02")
    order = m1.draft_purchase_order(PART, 1, "M-01", "wear")
    assert m1.find_free_engineers("cnc_tooling", at(0), at(48), 30)["error"].startswith("409")
    assert m2.find_free_engineers("cnc_tooling", at(0), at(48), 30)["slots"]  # M-01's order isn't M-02's repair
    assert m1.find_free_engineers("cnc_tooling", order["expected_delivery"], at(48), 30)["slots"]


def test_the_focus_names_the_machine_for_calls_without_a_header():
    factory = FakeFactory("fleet")
    http = admin(factory)
    assert http.post("/admin/focus", params={"machine_id": "M-03"}).json() == {"focus": "M-03"}
    factory.client().check_inventory(PART)
    machine(factory, "M-01").check_inventory(PART)  # the header wins
    assert [c["machine_id"] for c in http.get("/admin/calls").json()["calls"]] == ["M-03", "M-01"]
    http.post("/admin/reset", params={"scenario": "fleet"})
    assert factory.focus is None


def plan(status: str, part: str | None, summary: str) -> tuple[str, dict]:
    return ("submit_plan", {"status": status, "diagnosis": "Worn.", "repair_steps": ["Replace it"], "part_number": part,
                            "escalation_reason": None, "operator_summary": summary})


def from_stock_script(machine_id: str) -> list:
    return [
        ("search_manual", {"query": "tool wear"}),
        ("check_inventory", {"part_number": PART}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": at(0), "latest_end": at(48), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": "E-01", "machine_id": machine_id, "start": at(7), "end": at(7.5), "task": "swap"}),
        plan("awaiting_approval", PART, "Priya fits the spare tool today."),
    ]


def ordering_script(machine_id: str, po_id: str, engineer: str, hours: float, failure_h: float) -> list:
    return [
        ("search_manual", {"query": "tool wear"}),
        ("check_inventory", {"part_number": PART}),
        ("get_supplier_quote", {"part_number": PART}),
        ("draft_purchase_order", {"part_number": PART, "quantity": 1, "machine_id": machine_id, "reason": "wear"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": at(24), "latest_end": at(failure_h), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": engineer, "machine_id": machine_id, "start": at(hours), "end": at(hours + 0.5),
                           "task": "swap", "purchase_order_id": po_id}),
        plan("awaiting_approval", PART, "A new tool is ordered and an engineer is booked after it arrives."),
    ]


def pump_script() -> list:
    return [
        ("search_manual", {"query": "coolant pump"}),
        ("check_inventory", {"part_number": "CP-220"}),
        ("get_supplier_quote", {"part_number": "CP-220"}),
        plan("escalated", "CP-220", "The pump can't arrive before the failure."),
    ]


def test_a_scripted_fleet_shares_the_factory_and_passes_the_fleet_check():
    factory = FakeFactory("fleet")
    scripts = {"M-01": from_stock_script("M-01"), "M-02": ordering_script("M-02", "PO-0001", "E-01", 26, 40),
               "M-03": ordering_script("M-03", "PO-0002", "E-03", 27, 44), "M-04": pump_script()}
    results = {}
    for fleet_machine in FLEET.machines:  # one after another, so the scripted IDs are known
        agent = LocalAgent(llm=ScriptedLLM(scripts[fleet_machine.machine_id]), factory=factory.client(),
                           manual_sections=MANUAL, model="test")
        results[fleet_machine.machine_id] = runner.run(fleet_machine.alert(), agent=agent, backend="local")
    assert results["M-01"].part_in_stock and factory.bookings["BK-0001"]["reserved_part"] == PART  # filled in
    assert results["M-02"].part_in_stock is False  # M-01 holds the only one
    assert check_fleet(results, factory) == []
    factory.bookings["BK-0002"]["start"] = at(7)  # what a broken factory would allow
    assert any("booked twice" in p for p in check_fleet(results, factory))


def alert_for(machine_id: str) -> dict:
    return next(m for m in FLEET.machines if m.machine_id == machine_id).alert().model_dump(mode="json")


def test_incidents_are_kept_per_machine(monkeypatch):
    monkeypatch.setattr(api, "incidents", {})
    factory = FakeFactory("fleet")
    scripts = {"M-02": ordering_script("M-02", "PO-0001", "E-01", 26, 40), "M-01": from_stock_script("M-01")}
    monkeypatch.setattr(runner, "make_agent", lambda backend=None: LocalAgent(
        llm=ScriptedLLM(scripts.pop(next(iter(scripts)))), factory=factory.client(), manual_sections=MANUAL, model="test"))
    client = TestClient(api.app)
    assert client.get("/incidents").json() == {"incidents": []}
    client.post("/incidents", json=alert_for("M-02"))
    client.post("/incidents", json=alert_for("M-01"))
    listed = client.get("/incidents").json()["incidents"]
    assert [i["alert"]["machine_id"] for i in listed] == ["M-01", "M-02"]  # sorted by machine
    assert client.get("/incidents/M-02").json()["result"]["purchase_order_id"] == "PO-0001"
    assert client.get("/incidents/latest").json()["alert"]["machine_id"] == "M-01"  # the most recent alert
    assert client.get("/incidents/M-09").status_code == 404
    assert client.post("/incidents/replan", json={"feedback": "x", "machine_id": "M-09"}).status_code == 404
    assert client.delete("/incidents").json() == {"cleared": 2}
    assert client.get("/incidents/latest").status_code == 404


def test_replan_targets_the_named_machine(monkeypatch):
    monkeypatch.setattr(api, "incidents", {})
    factory = FakeFactory("fleet")
    scripts = [from_stock_script("M-01"), ordering_script("M-02", "PO-0001", "E-01", 26, 40),
               [("search_manual", {"query": "tool wear"}), ("check_inventory", {"part_number": PART}),
                ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": at(0), "latest_end": at(48), "duration_minutes": 30}),
                ("draft_booking", {"engineer_id": "E-01", "machine_id": "M-01", "start": at(26), "end": at(26.5), "task": "swap"}),
                plan("awaiting_approval", PART, "Moved to tomorrow.")]]
    monkeypatch.setattr(runner, "make_agent", lambda backend=None: LocalAgent(
        llm=ScriptedLLM(scripts.pop(0)), factory=factory.client(), manual_sections=MANUAL, model="test"))
    client = TestClient(api.app)
    client.post("/incidents", json=alert_for("M-01"))
    client.post("/incidents", json=alert_for("M-02"))  # now the most recent alert
    booking = factory.bookings["BK-0001"]
    admin(factory).post("/calendar/bookings/BK-0001/reject")
    admin(factory).post("/calendar/bookings/BK-0002/reject")  # frees E-01 at +26 for M-01's new plan
    response = client.post("/incidents/replan", json={"feedback": "Not today", "machine_id": "M-01",
                                                      "booking": {**booking, "status": "rejected"}})
    assert response.status_code == 200 and response.json()["machine_id"] == "M-01"
    assert client.get("/incidents/M-01").json()["feedback"] == "Not today"
    assert client.get("/incidents/M-02").json()["feedback"] is None  # untouched
