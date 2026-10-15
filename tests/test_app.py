from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import server
from devtools.fakes import PART, FakeFactory


@pytest.fixture
def screens(monkeypatch):
    factory = FakeFactory()
    latest = {"alert": {"machine_id": "M-01"}, "result": None, "received_at": "2026-10-14T08:00:00+00:00"}

    def agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=latest) if latest else httpx.Response(404, json={"detail": "no alert yet"})

    monkeypatch.setattr(server, "clients", {
        "agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent)),
        "mocks": httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle)),
    })
    return TestClient(server.app), factory, latest


def test_page_and_latest_plan(screens):
    client, _, latest = screens
    assert "Maintenance approval" in client.get("/").text
    page = client.get("/app.js")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-cache"
    assert client.get("/api/latest").json()["result"] is None  # the agent is still planning
    latest.clear()
    assert client.get("/api/latest").json() == {"alert": None, "result": None}


def test_manager_approves_order_and_booking(screens):
    client, factory, _ = screens
    order = factory.client().draft_purchase_order(PART, 1, "M-01", "wear")
    booking = factory.client().draft_booking("E-01", "M-01", "2026-10-15T14:00:00Z", "2026-10-15T14:30:00Z", "swap",
                                             order["po_id"])
    assert client.post(f"/api/purchase-orders/{order['po_id']}/approve").json()["status"] == "approved"
    assert client.post(f"/api/bookings/{booking['booking_id']}/reject").json()["status"] == "rejected"
    assert client.get(f"/api/bookings/{booking['booking_id']}").json()["status"] == "rejected"
    assert client.post(f"/api/purchase-orders/{order['po_id']}/approve").status_code == 409  # already decided


def test_only_approve_and_reject_reach_the_mocks(screens):
    client, _, _ = screens
    assert client.post("/api/purchase-orders/PO-0001/delete").status_code == 404
    assert client.post("/api/inventory/EM-1004/approve").status_code == 404


def test_reject_with_a_reason_asks_the_agent_to_replan(monkeypatch):
    factory = FakeFactory()
    order = factory.client().draft_purchase_order(PART, 1, "M-01", "wear")
    booking = factory.client().draft_booking("E-01", "M-01", "2026-10-15T14:00:00Z", "2026-10-15T14:30:00Z", "swap",
                                             order["po_id"])
    sent = []

    def agent(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/incidents/replan":
            sent.append(httpx.Response(200, content=request.content).json())
            return httpx.Response(200, json={"status": "awaiting_approval"})
        return httpx.Response(200, json={"alert": {}, "result": {"purchase_order_id": order["po_id"],
                                                                 "booking_id": booking["booking_id"]}})

    monkeypatch.setattr(server, "clients", {
        "agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent)),
        "mocks": httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle)),
    })

    class Now:  # run the background replan inline
        def __init__(self, target, args, daemon):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(server.threading, "Thread", Now)
    client = TestClient(server.app)
    assert client.post(f"/api/bookings/{booking['booking_id']}/reject", json={"reason": "Priya is on leave"}).status_code == 200
    assert sent == [{"feedback": "Priya is on leave", "purchase_order": factory.orders[order["po_id"]],
                     "booking": factory.bookings[booking["booking_id"]]}]
    assert sent[0]["booking"]["status"] == "rejected"
    client.post(f"/api/purchase-orders/{order['po_id']}/reject")  # no reason: no replan
    assert len(sent) == 1


def delay_setup(monkeypatch, booking_status=None, start="2026-10-15T14:00:00Z"):
    """An order (delivery 15 Oct 08:00) and a booking at `start` (a free slot), the latest plan pointing at both; replans recorded."""
    factory = FakeFactory()
    order = factory.client().draft_purchase_order(PART, 1, "M-01", "wear")
    booking = factory.client().draft_booking("E-01", "M-01", start, start.replace(":00:00Z", ":30:00Z"), "swap",
                                             order["po_id"])
    if booking_status:
        factory.handle(httpx.Request("POST", f"http://mocks/calendar/bookings/{booking['booking_id']}/{booking_status}"))
    sent = []

    def agent(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/incidents/replan":
            sent.append(httpx.Response(200, content=request.content).json())
            return httpx.Response(200, json={"status": "awaiting_approval"})
        return httpx.Response(200, json={"alert": {}, "result": {"purchase_order_id": order["po_id"],
                                                                 "booking_id": booking["booking_id"]}})

    monkeypatch.setattr(server, "clients", {
        "agent": httpx.Client(base_url="http://agent", transport=httpx.MockTransport(agent)),
        "mocks": httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle)),
    })

    class Now:  # run the background replan inline
        def __init__(self, target, args, daemon):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    monkeypatch.setattr(server.threading, "Thread", Now)
    return TestClient(server.app), factory, order, booking, sent


def test_delay_cancels_a_pending_booking_and_replans(monkeypatch):
    client, factory, order, booking, sent = delay_setup(monkeypatch)
    reply = client.post(f"/api/purchase-orders/{order['po_id']}/delay", json={"hours": 7}).json()
    assert reply["action"] == "replanning" and "now arrives Thu 15 Oct 15:00 UTC" in reply["message"]
    assert factory.bookings[booking["booking_id"]]["status"] == "rejected"
    assert len(sent) == 1 and "delayed PO-0001" in sent[0]["feedback"]
    assert sent[0]["purchase_order"]["expected_delivery"] == "2026-10-15T15:00:00+00:00"
    assert sent[0]["booking"]["status"] == "rejected"


def test_delay_leaves_an_approved_booking_to_the_manager(monkeypatch):
    client, factory, order, booking, sent = delay_setup(monkeypatch, booking_status="approve")
    reply = client.post(f"/api/purchase-orders/{order['po_id']}/delay", json={"hours": 7}).json()
    assert reply["action"] == "manager_must_decide" and "Reject the booking" in reply["message"]
    assert factory.bookings[booking["booking_id"]]["status"] == "approved" and sent == []


def test_delay_that_still_fits_changes_nothing(monkeypatch):
    client, factory, order, booking, sent = delay_setup(monkeypatch)
    reply = client.post(f"/api/purchase-orders/{order['po_id']}/delay", json={"hours": 2}).json()
    assert reply["action"] == "none" and factory.bookings[booking["booking_id"]]["status"] == "pending_approval"
    assert sent == []
    assert client.post(f"/api/purchase-orders/{order['po_id']}/delay", json={"hours": -1}).status_code == 422
