"""Fakes for tests and the scenario eval: the mock factory with pinned test data, a short manual, and a
scripted LLM. Dates are pinned to the sample alert (detected 14 Oct 08:00 UTC, failure 16 Oct 08:00).

The demo uses mocks/seed.json and manual/ instead."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx

from agent.manual import Section
from agent.models import Alert
from mcp_server.factory import FactoryClient
from mocks.factory import MockFactory

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

_SUPPLIER = {"supplier": "ToolCo", "unit_price": 89.0, "currency": "EUR", "lead_time_hours": 24}
FAKE_SEED = {
    "note": "Test data pinned to the sample alert.",
    "parts": [
        {"part_number": PART, "name": "Carbide end mill 10 mm 4-flute", "location": "B-3",
         "stock": {"out_of_stock": 0, "in_stock": 2, "no_engineer": 0}, "supplier": _SUPPLIER},
        {"part_number": "EM-1006", "name": "Carbide end mill 6 mm 2-flute", "location": "B-3",
         "stock": dict.fromkeys(SLOTS, 5), "supplier": _SUPPLIER},
    ],
    "engineers": [{"engineer_id": "E-01", "name": "Priya Nair", "skills": ["cnc_tooling"]}],
    "slots": {
        scenario: [{"engineer_id": "E-01", "hours_after_reset": (datetime.fromisoformat(s) - NOW) / timedelta(hours=1)}
                   for s in starts]
        for scenario, starts in SLOTS.items()
    },
}


class FakeFactory(MockFactory):
    """The mock factory systems with test seed data and the clock pinned to the sample alert (NOW)."""

    def __init__(self, scenario: str = "out_of_stock"):
        super().__init__(FAKE_SEED, scenario, clock=lambda: NOW)

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
