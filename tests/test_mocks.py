from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from mcp_server.factory import FactoryClient
from mocks import server
from mocks.factory import MockFactory, load_seed

START = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
DETECTED = START + timedelta(minutes=20)  # reset just before the replay; the alert follows
CUTTING_MIN_PER_DAY = 20  # detector default


def demo(scenario: str, now: datetime = DETECTED) -> tuple[MockFactory, FactoryClient]:
    clock = {"now": START + timedelta(minutes=7)}
    factory = MockFactory(load_seed(), scenario, clock=lambda: clock["now"])
    clock["now"] = now
    return factory, FactoryClient(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))


def window(remaining_cutting_min: float) -> tuple[str, str]:
    failure = DETECTED + timedelta(days=remaining_cutting_min / CUTTING_MIN_PER_DAY)
    return (DETECTED + timedelta(hours=24)).isoformat(), failure.isoformat()


def test_server_serves_contract_and_resets():
    client = TestClient(server.app)
    assert client.post("/admin/reset", params={"scenario": "in_stock"}).json()["scenario"] == "in_stock"
    assert client.get("/inventory/parts/EM-1004").json()["quantity_on_hand"] == 2
    assert client.post("/admin/reset", params={"scenario": "out_of_stock"}).status_code == 200
    assert client.get("/inventory/parts/EM-1004").json()["quantity_on_hand"] == 0
    assert client.get("/inventory/parts/T-789").status_code == 404
    assert client.post("/admin/reset", params={"scenario": "nope"}).status_code == 400
    assert client.get("/health").json()["scenario"] == "out_of_stock"


def test_slots_are_whole_hours_after_reset():
    factory, _ = demo("out_of_stock")
    assert factory.started == START
    assert [s["start"] for s in factory.slots][:2] == [START + timedelta(hours=7), START + timedelta(hours=30)]


def test_demo_seed_supports_each_scenario_for_the_alert_range():
    """The detector alerts with about 25–45 cutting minutes left: each scenario must still behave as named."""
    for remaining in (26, 38, 45):
        for scenario in ("out_of_stock", "no_engineer"):
            _, client = demo(scenario)
            order = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")
            assert order["expected_delivery"] == (START + timedelta(hours=25)).isoformat()  # rounded up to the hour
            earliest, latest = window(remaining)
            slots = client.find_free_engineers("cnc_tooling", order["expected_delivery"], latest, 30)["slots"]
            assert bool(slots) == (scenario == "out_of_stock"), (scenario, remaining)
        _, client = demo("in_stock")
        assert client.check_inventory("EM-1004")["quantity_on_hand"] > 0
        assert client.find_free_engineers("cnc_tooling", DETECTED.isoformat(), window(remaining)[1], 30)["slots"]


def test_skill_filter_and_quote():
    _, client = demo("out_of_stock")
    later = (START + timedelta(days=3)).isoformat()
    mechanical = client.find_free_engineers("mechanical", START.isoformat(), later, 30)["slots"]
    assert {s["name"] for s in mechanical} == {"Daniel Tan"}
    offers = client.get_supplier_quote("EM-1004")["offers"]
    assert [(o["unit_price"], o["lead_time_hours"]) for o in offers] == [(89.0, 24), (145.0, 6)]  # cheapest first
    assert offers[1]["expected_delivery"] == (START + timedelta(hours=7)).isoformat()  # +6 h, rounded up to the hour
    assert "error" in client.draft_purchase_order("T-789", 1, "M-01", "x")


def test_order_total_and_app_only_approval():
    factory, client = demo("out_of_stock")
    order = client.draft_purchase_order("EM-1004", 2, "M-01", "tool wear")
    assert order["status"] == "pending_approval" and order["total_price"] == 178.0
    http = httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))
    assert http.post(f"/supplier/purchase-orders/{order['po_id']}/approve").json()["status"] == "approved"
    assert http.post(f"/supplier/purchase-orders/{order['po_id']}/reject").status_code == 409
    assert http.get(f"/supplier/purchase-orders/{order['po_id']}").json()["status"] == "approved"


def test_approved_booking_takes_the_slot():
    factory, client = demo("in_stock")
    start = START + timedelta(hours=30)
    booked = client.draft_booking("E-01", "M-01", start.isoformat(), (start + timedelta(minutes=30)).isoformat(), "swap")
    http = httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))
    assert http.post(f"/calendar/bookings/{booked['booking_id']}/approve").json()["status"] == "approved"
    free = client.find_free_engineers("cnc_tooling", START.isoformat(), (START + timedelta(days=3)).isoformat(), 30)["slots"]
    assert start.isoformat() not in [s["slot_start"] for s in free]
    again = client.draft_booking("E-01", "M-01", start.isoformat(), (start + timedelta(minutes=30)).isoformat(), "swap")
    assert again["error"].startswith("409")


def test_agent_client_has_no_approval():
    assert not [name for name in dir(FactoryClient) if "approve" in name or "reject" in name]


def test_call_log_records_agent_calls_in_tool_shape():
    factory, client = demo("out_of_stock")
    http = httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))
    client.check_inventory("T-789")
    client.find_free_engineers("cnc_tooling", START.isoformat(), (START + timedelta(days=3)).isoformat(), 30)
    http.post("/admin/reset", params={"scenario": "in_stock"})  # not an agent call; clears the log
    client.check_inventory("EM-1004")
    log = http.get("/admin/calls", params={"after": 1}).json()
    assert log["last"] == 3
    assert [(c["seq"], c["tool"]) for c in log["calls"]] == [(3, "check_inventory")]
    assert log["calls"][0]["result"]["quantity_on_hand"] == 2
    assert factory.calls == log["calls"]


def test_call_log_keeps_errors_and_slot_shape():
    factory, client = demo("out_of_stock")
    http = httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))
    client.check_inventory("T-789")
    client.find_free_engineers("cnc_tooling", START.isoformat(), (START + timedelta(days=3)).isoformat(), 30)
    first, search = http.get("/admin/calls").json()["calls"]
    assert first["result"] == {"error": "404: unknown part"}
    assert search["arguments"]["duration_minutes"] == 30 and len(search["result"]["slots"]) == 3  # Priya x2, Wei Ling


def test_search_before_the_ordered_part_arrives_is_refused():
    _, client = demo("out_of_stock")
    later = (START + timedelta(days=3)).isoformat()
    assert client.find_free_engineers("cnc_tooling", START.isoformat(), later, 30)["slots"]  # no order yet
    order = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")
    refused = client.find_free_engineers("cnc_tooling", START.isoformat(), later, 30)
    assert refused["error"].startswith("409") and order["expected_delivery"] in refused["error"]
    assert client.find_free_engineers("cnc_tooling", order["expected_delivery"], later, 30)["slots"]


def test_one_open_order_per_part_and_machine():
    factory, client = demo("out_of_stock")
    first = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")
    again = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")
    assert again["error"].startswith("409") and first["po_id"] in again["error"]
    assert "error" not in client.draft_purchase_order("EM-1006", 1, "M-01", "finishing tool")  # another part
    http = httpx.Client(base_url="http://mocks", transport=httpx.MockTransport(factory.handle))
    http.post(f"/supplier/purchase-orders/{first['po_id']}/reject")
    assert "error" not in client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")  # the old one was rejected



def test_order_from_a_chosen_supplier():
    _, client = demo("urgent")
    fast = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear", supplier="Example Tooling Partner (fictional)")
    assert fast["total_price"] == 145.0
    assert fast["expected_delivery"] == (START + timedelta(hours=7)).isoformat()  # 09:20 + 6 h, rounded up to 16:00
    assert "does not sell" in client.draft_purchase_order("EM-1006", 1, "M-01", "x", supplier="Example Tooling Partner (fictional)")["error"]


def test_urgent_demo_needs_the_fast_supplier():
    """Detector with CUTTING_MIN_PER_DAY=60 and ~16 cutting minutes left: failure about 16 h after the alert."""
    failure = (DETECTED + timedelta(hours=16)).isoformat()
    _, client = demo("urgent")
    offers = client.get_supplier_quote("EM-1004")["offers"]
    cheap, fast = offers[0]["expected_delivery"], offers[1]["expected_delivery"]
    assert cheap > failure  # too late
    assert client.find_free_engineers("cnc_tooling", fast, failure, 30)["slots"]


def test_empty_search_window_is_refused():
    """Searching from a delivery after the predicted failure: say why, instead of an empty list."""
    _, client = demo("urgent")
    cheap = client.get_supplier_quote("EM-1004")["offers"][0]["expected_delivery"]
    failure = (DETECTED + timedelta(hours=16)).isoformat()
    refused = client.find_free_engineers("cnc_tooling", cheap, failure, 30)
    assert refused["error"].startswith("422: the window is empty") and "faster supplier" in refused["error"]


def test_supplier_delay_moves_the_delivery_and_keeps_the_original():
    factory, client = demo("out_of_stock")
    order = client.draft_purchase_order("EM-1004", 1, "M-01", "tool wear")
    calls = len(factory.calls)

    def delay(po_id, hours):
        return factory.handle(httpx.Request("POST", f"http://mocks/supplier/purchase-orders/{po_id}/delay",
                                            params={"hours": hours}))

    first = delay(order["po_id"], 2.5).json()
    original = datetime.fromisoformat(order["expected_delivery"])
    assert first["expected_delivery"] == (original + timedelta(hours=3)).isoformat()  # rounded up to the hour
    second = delay(order["po_id"], 1).json()
    assert second["original_delivery"] == order["expected_delivery"]  # the first date is kept
    assert second["expected_delivery"] == (original + timedelta(hours=4)).isoformat()
    assert delay(order["po_id"], 0).status_code == 422 and delay("PO-9999", 1).status_code == 404
    assert len(factory.calls) == calls  # a supplier event, not an agent call
    factory.handle(httpx.Request("POST", f"http://mocks/supplier/purchase-orders/{order['po_id']}/reject"))
    assert delay(order["po_id"], 1).status_code == 409


def test_server_passes_the_machine_header_through():
    """Over real HTTP (not only MockTransport), X-Machine-Id must reach the factory: it scopes the delivery rule,
    reservations and the call log to the calling machine. Lost, M-04's slow pump order blocked M-01's search."""
    client = TestClient(server.app)
    client.post("/admin/reset", params={"scenario": "fleet"})
    m4, m1 = {"X-Machine-Id": "M-04"}, {"X-Machine-Id": "M-01"}
    order = client.post("/supplier/purchase-orders", headers=m4,
                        json={"part_number": "CP-220", "quantity": 1, "machine_id": "M-04", "reason": "pump"}).json()
    start = order["expected_delivery"]  # 72 h away
    early = {"skill": "cnc_tooling", "earliest_start": "2020-01-01T00:00:00+00:00", "latest_end": start,
             "duration_minutes": 30}
    assert client.get("/calendar/engineers/free", headers=m1, params=early).status_code == 200  # not M-01's order
    assert client.get("/calendar/engineers/free", headers=m4, params=early).status_code == 409  # M-04's own
    assert [c["machine_id"] for c in client.get("/admin/calls").json()["calls"]] == ["M-04", "M-01", "M-04"]
