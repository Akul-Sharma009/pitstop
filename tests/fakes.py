"""Test doubles: an in-memory factory implementing docs/contracts.md, a manual, and a scripted LLM."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx

from agent.manual import Section
from mcp_server.factory import FactoryClient

PART = "EM-1004"
NOW = datetime.fromisoformat("2026-10-14T08:00:00+00:00")
FAILURE = datetime.fromisoformat("2026-10-16T08:00:00+00:00")

MANUAL = [
    Section(
        "cnc_mill_m01.md",
        "Tool wear",
        f"Symptom: rising torque. Fix: replace the end mill, part {PART} (Carbide end mill 10 mm 4-flute). "
        "Repair time 30 minutes. Skill: cnc_tooling.",
    ),
    Section("cnc_mill_m01.md", "Coolant pump", "Replace pump CP-220 if flow drops."),
]


class FakeFactory:
    """In-memory mock systems for one scenario: out_of_stock, in_stock or no_engineer."""

    def __init__(self, scenario: str = "out_of_stock"):
        self.stock = 2 if scenario == "in_stock" else 0
        self.slot_start = None if scenario == "no_engineer" else datetime.fromisoformat("2026-10-15T14:00:00+00:00")
        self.orders: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path.startswith("/inventory/parts/"):
            if path.rsplit("/", 1)[1] != PART:
                return httpx.Response(404, json={"detail": "unknown part"})
            return httpx.Response(200, json={"part_number": PART, "name": "Carbide end mill", "quantity_on_hand": self.stock, "location": "B-3"})
        if method == "GET" and path.startswith("/supplier/parts/"):
            return httpx.Response(200, json={"part_number": PART, "supplier": "ToolCo", "unit_price": 89.0, "currency": "EUR", "lead_time_hours": 24})
        if method == "POST" and path == "/supplier/purchase-orders":
            body = json.loads(request.content)
            po_id = f"PO-{len(self.orders) + 1:04d}"
            order = {**body, "po_id": po_id, "status": "pending_approval", "expected_delivery": (NOW + timedelta(hours=24)).isoformat()}
            self.orders[po_id] = order
            return httpx.Response(200, json=order)
        if method == "GET" and path == "/calendar/engineers/free":
            if self.slot_start is None:
                return httpx.Response(200, json=[])
            end = self.slot_start + timedelta(minutes=int(request.url.params["duration_minutes"]))
            return httpx.Response(200, json=[{"engineer_id": "E-01", "name": "Priya Nair", "skills": ["cnc_tooling"], "slot_start": self.slot_start.isoformat(), "slot_end": end.isoformat()}])
        if method == "POST" and path == "/calendar/bookings":
            body = json.loads(request.content)
            booking_id = f"BK-{len(self.bookings) + 1:04d}"
            booking = {**body, "booking_id": booking_id, "status": "pending_approval"}
            self.bookings[booking_id] = booking
            return httpx.Response(200, json=booking)
        return httpx.Response(404, json={"detail": "not found"})

    def client(self) -> FactoryClient:
        return FactoryClient(base_url="http://mocks", transport=httpx.MockTransport(self.handle))


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


class ScriptedLLM:
    """Stands in for the OpenAI client: replies with one scripted tool call per turn."""

    def __init__(self, steps: list[tuple[str, dict[str, Any]] | str]):
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, str):
            message = SimpleNamespace(content=step, tool_calls=None)
        else:
            message = SimpleNamespace(content="", tool_calls=[_call(f"call-{len(self.requests)}", *step)])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def thin_path_script() -> list[tuple[str, dict[str, Any]]]:
    """What a well-behaved model does in the out_of_stock scenario."""
    return [
        ("search_manual", {"query": "tool wear end mill replacement"}),
        ("check_inventory", {"part_number": PART}),
        ("get_supplier_quote", {"part_number": PART}),
        ("draft_purchase_order", {"part_number": PART, "quantity": 1, "machine_id": "M-01", "reason": "Tool wear"}),
        ("find_free_engineers", {"skill": "cnc_tooling", "earliest_start": "2026-10-15T08:00:00+00:00", "latest_end": FAILURE.isoformat(), "duration_minutes": 30}),
        ("draft_booking", {"engineer_id": "E-01", "machine_id": "M-01", "start": "2026-10-15T14:00:00+00:00", "end": "2026-10-15T14:30:00+00:00", "task": "Replace end mill", "purchase_order_id": "PO-0001"}),
        ("submit_plan", {
            "status": "awaiting_approval",
            "diagnosis": "The cutting tool is nearly worn out.",
            "repair_steps": ["Lock out the machine", f"Replace end mill {PART}"],
            "part_number": PART,
            "part_name": "Carbide end mill 10 mm 4-flute",
            "part_in_stock": False,
            "purchase_order_id": "PO-0001",
            "expected_delivery": "2026-10-15T08:00:00+00:00",
            "booking_id": "BK-0001",
            "engineer_name": "Priya Nair",
            "booking_start": "2026-10-15T14:00:00+00:00",
            "booking_end": "2026-10-15T14:30:00+00:00",
            "operator_summary": "The cutting tool on M-01 is wearing out. A new one is ordered and Priya will fit it tomorrow afternoon, once the manager approves.",
        }),
    ]
