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
    assert client.get("/app.js").status_code == 200
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
