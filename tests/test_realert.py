"""A new alert for a machine that already has a plan: a repeat keeps the plan; a worse reading replans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from agent import api, runner
from agent.times import utc
from agent.local_agent import LocalAgent
from app import server
from devtools.fakes import MANUAL, PART, FakeFactory, ScriptedLLM, sample_alert, thin_path_script


def agent_service(monkeypatch, statuses=None):
    """The agent API with one scripted run per alert; draft statuses as the mock factory would report them."""
    monkeypatch.setattr(api, "incidents", {})
    runs = []

    def make(backend=None):
        runs.append(1)
        return LocalAgent(llm=ScriptedLLM(thin_path_script()), factory=FakeFactory().client(), manual_sections=MANUAL,
                          model="test")

    monkeypatch.setattr(runner, "make_agent", make)
    monkeypatch.setattr(api, "draft_status", lambda kind, item_id: (statuses or {}).get(kind, "pending_approval"))
    return TestClient(api.app), runs


def test_a_repeat_alert_keeps_the_open_plan(monkeypatch):
    client, runs = agent_service(monkeypatch)
    alert = sample_alert().model_dump(mode="json")
    first = client.post("/incidents", json=alert).json()
    again = client.post("/incidents", json={**alert, "risk": 0.95}).json()
    assert len(runs) == 1  # the agent didn't run again: no second booking, the manager isn't asked twice
    assert again["booking_id"] == first["booking_id"] and again["warnings"] == []
    incident = client.get("/incidents/M-01").json()
    assert incident["repeats"] == 1 and incident["alert"]["risk"] == 0.95


def test_a_worse_alert_keeps_the_plan_but_warns_the_manager(monkeypatch):
    """The agent service never cancels a booking: only the manager (or the app's demo control) does."""
    client, runs = agent_service(monkeypatch)
    alert = sample_alert().model_dump(mode="json")
    first = client.post("/incidents", json=alert).json()  # booked Thu 15 Oct 14:00–14:30
    sooner = {**alert, "predicted_failure_at": "2026-10-15T12:00:00+00:00"}
    kept = client.post("/incidents", json=sooner).json()
    assert len(runs) == 1 and kept["booking_id"] == first["booking_id"]
    assert any("before the booked repair ends" in w and f"Reject booking {first['booking_id']}" in w for w in kept["warnings"])
    assert utc(client.get("/incidents/M-01").json()["alert"]["predicted_failure_at"]) == utc(sooner["predicted_failure_at"])


def test_a_new_alert_plans_afresh_when_the_plan_is_not_open(monkeypatch):
    client, runs = agent_service(monkeypatch, statuses={"booking": "rejected"})
    alert = sample_alert().model_dump(mode="json")
    client.post("/incidents", json=alert)
    client.post("/incidents", json=alert)
    assert len(runs) == 2  # the booking was rejected without a reason: nothing to keep
    client.post("/incidents", json={**alert, "failure_type": "coolant_pump"})
    assert len(runs) == 3  # another failure is another plan


def test_an_alert_while_planning_is_refused(monkeypatch):
    client, _ = agent_service(monkeypatch)
    api.incidents["M-01"] = {"alert": {}, "result": None, "received_at": "x"}
    response = client.post("/incidents", json=sample_alert().model_dump(mode="json"))
    assert response.status_code == 409 and "still planning" in response.json()["detail"]


def test_a_replan_with_a_newer_alert_plans_for_it(monkeypatch):
    client, runs = agent_service(monkeypatch)
    alert = sample_alert().model_dump(mode="json")
    client.post("/incidents", json=alert)
    sooner = {**alert, "predicted_failure_at": "2026-10-15T20:00:00+00:00", "simulated": True}
    response = client.post("/incidents/replan", json={"feedback": "worse", "machine_id": "M-01", "alert": sooner})
    assert response.status_code == 200 and len(runs) == 2
    assert utc(client.get("/incidents/M-01").json()["alert"]["predicted_failure_at"]) == utc(sooner["predicted_failure_at"])


NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)  # the fake factory's time


def screens(monkeypatch, booking_status=None):
    """The app with M-01's plan: an order and a booking Thu 15 Oct 14:00–14:30; replans and alerts recorded."""
    factory = FakeFactory()
    order = factory.client().draft_purchase_order(PART, 1, "M-01", "wear")
    booking = factory.client().draft_booking("E-01", "M-01", "2026-10-15T14:00:00Z", "2026-10-15T14:30:00Z", "swap",
                                             order["po_id"])
    if booking_status:
        factory.handle(httpx.Request("POST", f"http://mocks/calendar/bookings/{booking['booking_id']}/{booking_status}"))
    alert = {**sample_alert().model_dump(mode="json"), "tool_wear_min": 172, "remaining_cutting_min": 40}
    plan = {"alert": alert, "result": {"purchase_order_id": order["po_id"], "booking_id": booking["booking_id"],
                                       "booking_start": "2026-10-15T14:00:00Z"}}
    sent = {"replan": [], "alert": []}

    def agent(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            sent["replan" if request.url.path == "/incidents/replan" else "alert"].append(
                httpx.Response(200, content=request.content).json())
            return httpx.Response(200, json={"status": "awaiting_approval"})
        return httpx.Response(200, json=plan) if request.url.path.endswith("M-01") else httpx.Response(404, json={})

    monkeypatch.setattr(server, "clients", {
        "agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent)),
        "mocks": httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle)),
    })
    monkeypatch.setattr(server, "clock", lambda: NOW)

    class Now:  # run the background work inline
        def __init__(self, target, args, daemon):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(server.threading, "Thread", Now)
    return TestClient(server.app), factory, booking, sent


def test_worse_reading_cancels_a_pending_booking_and_replans_for_the_new_alert(monkeypatch):
    client, factory, booking, sent = screens(monkeypatch)
    reply = client.post("/api/incidents/M-01/worse", json={}).json()  # default: an hour before the booked repair
    assert reply["action"] == "replanning" and factory.bookings[booking["booking_id"]]["status"] == "rejected"
    new = sent["replan"][0]["alert"]
    assert new["predicted_failure_at"] == (NOW + timedelta(hours=29)).isoformat() and new["simulated"] is True
    assert new["remaining_cutting_min"] < 40 and new["tool_wear_min"] + new["remaining_cutting_min"] == 212  # same limit
    feedback = sent["replan"][0]["feedback"]
    assert "Keep PO-0001" in feedback and booking["booking_id"] in feedback and sent["alert"] == []


def test_worse_reading_leaves_an_approved_booking_to_the_manager(monkeypatch):
    client, factory, booking, sent = screens(monkeypatch, booking_status="approve")
    reply = client.post("/api/incidents/M-01/worse", json={"failure_in_h": 20}).json()
    assert reply["action"] == "manager_must_decide" and factory.bookings[booking["booking_id"]]["status"] == "approved"
    assert sent["replan"] == [] and len(sent["alert"]) == 1  # the agent keeps the plan and warns


def test_worse_reading_that_still_fits_only_sends_the_alert(monkeypatch):
    client, factory, booking, sent = screens(monkeypatch)
    reply = client.post("/api/incidents/M-01/worse", json={"failure_in_h": 40}).json()
    assert reply["action"] == "alert_sent" and factory.bookings[booking["booking_id"]]["status"] == "pending_approval"
    assert sent["replan"] == [] and len(sent["alert"]) == 1
    assert client.post("/api/incidents/M-01/worse", json={"failure_in_h": 0}).status_code == 422
    assert client.post("/api/incidents/M-09/worse", json={}).status_code == 404
