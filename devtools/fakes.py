"""Fakes for tests and dev runs: an in-memory factory implementing docs/contracts.md, a short manual,
and a scripted LLM. Dates are pinned to the sample alert (detected 14 Oct 08:00 UTC, failure 16 Oct 08:00).

These stand in until mocks/ and manual/ exist; they are not the demo's mock systems."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx

from agent.manual import Section
from agent.models import Alert
from mcp_server.factory import FactoryClient

PART = "EM-1004"
NOW = datetime.fromisoformat("2026-10-14T08:00:00+00:00")
FAILURE = datetime.fromisoformat("2026-10-16T08:00:00+00:00")

MANUAL = [
    Section(
        "cnc_mill_m01.md",
        "Tool wear",
        "Symptoms: rising spindle torque, poor surface finish, tool wear above 200 minutes.\n"
        f"Fix: replace the main end mill, part {PART} (carbide end mill 10 mm 4-flute).\n"
        "Steps: 1. Stop the spindle and lock out the machine. 2. Remove the tool holder. "
        "3. Fit the new end mill and set the tool length offset. 4. Run a test cut.\n"
        "Repair time: 30 minutes. Required skill: cnc_tooling.",
    ),
    Section(
        "cnc_mill_m01.md",
        "Finishing tool",
        "The finishing end mill, part EM-1006 (carbide end mill 6 mm 2-flute), is only used for finishing passes. "
        "Replace it when finishing quality drops.",
    ),
    Section("cnc_mill_m01.md", "Coolant pump", "Replace pump CP-220 if coolant flow drops. Required skill: mechanical."),
]

# Engineer E-01's free slots. Only those fully inside the requested window are returned.
SLOTS = {
    "out_of_stock": ["2026-10-14T15:00:00+00:00", "2026-10-15T14:00:00+00:00"],  # first is before delivery
    "in_stock": ["2026-10-14T15:00:00+00:00", "2026-10-15T14:00:00+00:00"],
    "no_engineer": ["2026-10-14T15:00:00+00:00", "2026-10-16T12:00:00+00:00"],  # none between delivery and failure
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class FakeFactory:
    """In-memory mock systems for one scenario: out_of_stock, in_stock or no_engineer."""

    def __init__(self, scenario: str = "out_of_stock"):
        self.reset(scenario)

    def reset(self, scenario: str) -> None:
        if scenario not in SLOTS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.scenario = scenario
        self.stock = {PART: 2 if scenario == "in_stock" else 0, "EM-1006": 5}
        self.slots = [datetime.fromisoformat(s) for s in SLOTS[scenario]]
        self.orders: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/admin/reset":
            try:
                self.reset(request.url.params.get("scenario", "out_of_stock"))
            except ValueError as exc:
                return httpx.Response(400, json={"detail": str(exc)})
            return httpx.Response(200, json={"scenario": self.scenario})
        if method == "GET" and path.startswith("/inventory/parts/"):
            part = path.rsplit("/", 1)[1]
            if part not in self.stock:
                return httpx.Response(404, json={"detail": "unknown part"})
            return httpx.Response(200, json={"part_number": part, "name": "Carbide end mill", "quantity_on_hand": self.stock[part], "location": "B-3"})
        if method == "GET" and path.startswith("/supplier/parts/"):
            part = path.rsplit("/", 1)[1]
            if part not in self.stock:
                return httpx.Response(404, json={"detail": "supplier does not sell this part"})
            return httpx.Response(200, json={"part_number": part, "supplier": "ToolCo", "unit_price": 89.0, "currency": "EUR", "lead_time_hours": 24})
        if method == "POST" and path == "/supplier/purchase-orders":
            body = json.loads(request.content)
            po_id = f"PO-{len(self.orders) + 1:04d}"
            order = {**body, "po_id": po_id, "status": "pending_approval", "expected_delivery": (NOW + timedelta(hours=24)).isoformat()}
            self.orders[po_id] = order
            return httpx.Response(200, json=order)
        if method == "GET" and path == "/calendar/engineers/free":
            params = request.url.params
            try:
                earliest = _utc(datetime.fromisoformat(params["earliest_start"]))
                latest = _utc(datetime.fromisoformat(params["latest_end"]))
                duration = timedelta(minutes=int(params["duration_minutes"]))
            except (KeyError, ValueError) as exc:
                return httpx.Response(422, json={"detail": f"bad query: {exc}"})
            if params.get("skill") != "cnc_tooling":
                return httpx.Response(200, json=[])
            free = [
                {"engineer_id": "E-01", "name": "Priya Nair", "skills": ["cnc_tooling"],
                 "slot_start": start.isoformat(), "slot_end": (start + duration).isoformat()}
                for start in self.slots
                if start >= earliest and start + duration <= latest
            ]
            return httpx.Response(200, json=free)
        if method == "POST" and path == "/calendar/bookings":
            body = json.loads(request.content)
            try:
                start = _utc(datetime.fromisoformat(body["start"]))
            except (KeyError, ValueError) as exc:
                return httpx.Response(422, json={"detail": f"bad booking: {exc}"})
            if body.get("engineer_id") != "E-01" or start not in self.slots:
                return httpx.Response(409, json={"detail": "engineer is not free at that time"})
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


def sample_alert() -> Alert:
    """The thin-path alert: M-01's tool is wearing out, failure predicted in two days."""
    return Alert(
        machine_id="M-01",
        failure_type="tool_wear",
        risk=0.93,
        tool_wear_min=172,
        remaining_cutting_min=38,
        detected_at=NOW,
        predicted_failure_at=FAILURE,
        readings={"air_temperature_k": 298.9, "process_temperature_k": 309.1, "rotational_speed_rpm": 1420, "torque_nm": 46.3},
    )
