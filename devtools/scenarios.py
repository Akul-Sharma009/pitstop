"""Demo and stress scenarios for the agent eval and the tests: what the factory looks like, the alert the
agent gets, and what a correct agent does. The fake factory's seed data is built from this table
(devtools/fakes.py), and scripts/eval_agent.py checks each run against the expectations.

Times are pinned: the alert arrives at NOW (Wed 14 Oct 08:00 UTC). Repair minutes and skills match the
machine manual (manual/cnc_mill_m01.md). Alerts for failure types other than tool wear are simulated: the
detector only flags tool wear (AI4I TWF).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.models import Alert

PART = "EM-1004"
NOW = datetime.fromisoformat("2026-10-14T08:00:00+00:00")
FAILURE = datetime.fromisoformat("2026-10-16T08:00:00+00:00")
READINGS = {"air_temperature_k": 298.9, "process_temperature_k": 309.1, "rotational_speed_rpm": 1420, "torque_nm": 46.3}

# Parts: name and supplier offers (supplier, unit price, lead time in hours), cheapest first.
# The suppliers (ToolCo, SpeedTool, EUR) differ from the demo seed (mocks/seed.json: the teammate's fictional
# suppliers, SGD) on purpose: the eval must not pass because the prompt was tuned on the demo's names.
# Engineers: id → (name, skills).
CHEAP, FAST = "ToolCo", "SpeedTool"
PARTS = {
    "EM-1004": ("Carbide end mill 10 mm 4-flute", [(CHEAP, 89.0, 24), (FAST, 145.0, 6)]),
    "EM-1006": ("Carbide end mill 6 mm 2-flute (finishing)", [(CHEAP, 64.0, 24)]),
    "CP-220": ("Coolant pump", [(CHEAP, 420.0, 72)]),
}
ENGINEERS = {"E-01": ("Priya Nair", ["cnc_tooling"]), "E-02": ("Daniel Tan", ["mechanical"]),
             "E-03": ("Wei Ling Ho", ["cnc_tooling"])}


@dataclass
class Previous:
    """For a replan: the plan the manager reviewed, and what they said."""

    feedback: str
    order_status: str  # the previous purchase order (for the scenario's part): pending_approval|approved|rejected
    booking: tuple[str, float]  # (engineer, slot hours after NOW) the previous plan booked
    booking_status: str = "rejected"
    avoid: tuple[str, ...] = ()  # engineers the feedback rules out


@dataclass
class Scenario:
    name: str
    about: str
    failure_type: str = "tool_wear"
    failure_in_h: float = 48
    tool_wear: tuple[float, float] | None = (172, 38)  # (tool wear, remaining cutting minutes), tool wear only
    stock: dict[str, int] = field(default_factory=dict)  # parts not listed: 0 on hand
    slots: dict[str, list[float]] = field(default_factory=dict)  # engineer → slot starts, hours after NOW
    # What a correct agent does:
    part: str | None = PART  # None: the manual lists no part, so nothing may be ordered
    order: bool | None = False  # True: must order `part`; None: ordering is optional (it can't help in time)
    supplier: str = CHEAP  # the offer a correct agent orders from: the cheapest that arrives in time
    booking: tuple[str, float] | None = None  # (engineer, slot hours after NOW) to book; None: must escalate
    search: tuple[str, int] | None = ("cnc_tooling", 30)  # (skill, repair minutes) the engineer search must use
    search_required: bool = True  # False: escalating without any engineer search is fine
    previous: Previous | None = None  # set for replan scenarios

    @property
    def failure_at(self) -> datetime:
        return NOW + timedelta(hours=self.failure_in_h)

    @property
    def delivery(self) -> datetime | None:
        """When the part arrives if ordered at NOW from the expected supplier."""
        if not self.part:
            return None
        lead = next(hours for name, _, hours in PARTS[self.part][1] if name == self.supplier)
        return NOW + timedelta(hours=lead)

    def alert(self) -> Alert:
        wear, remaining = self.tool_wear or (None, None)
        return Alert(machine_id="M-01", failure_type=self.failure_type, risk=0.93, tool_wear_min=wear,
                     remaining_cutting_min=remaining, detected_at=NOW, predicted_failure_at=self.failure_at,
                     readings=READINGS)


SCENARIOS = {s.name: s for s in [
    # The three demo scenarios (CLAUDE.md, "Scope").
    Scenario("out_of_stock", "Part out of stock: order it, book the engineer after delivery.",
             stock={"EM-1006": 5, "CP-220": 1}, slots={"E-01": [7, 30]}, order=True, booking=("E-01", 30)),
    Scenario("in_stock", "Part in stock: no order, book the earliest slot.",
             stock={PART: 2, "EM-1006": 5, "CP-220": 1}, slots={"E-01": [7, 30]}, booking=("E-01", 7)),
    Scenario("no_engineer", "No engineer free between delivery and failure: order, then escalate.",
             stock={"EM-1006": 5, "CP-220": 1}, slots={"E-01": [3, 52]}, order=True),
    # Stress scenarios: situations the prompt was not tuned on.
    Scenario("finishing_tool", "Look-alike part: the finishing tool is EM-1006, not EM-1004 (both in stock).",
             failure_type="finishing_tool", tool_wear=None, stock={PART: 2, "EM-1006": 3}, slots={"E-01": [7, 30]},
             part="EM-1006", booking=("E-01", 7), search=("cnc_tooling", 25)),
    Scenario("finishing_tool_out", "Look-alike part out of stock while EM-1004 is in stock: order EM-1006.",
             failure_type="finishing_tool", tool_wear=None, stock={PART: 2}, slots={"E-01": [7, 30]},
             part="EM-1006", order=True, booking=("E-01", 30), search=("cnc_tooling", 25)),
    Scenario("coolant_pump", "Other skill: the pump needs a mechanical engineer for 90 minutes.",
             failure_type="coolant_pump", tool_wear=None, stock={PART: 2, "CP-220": 1},
             slots={"E-01": [3], "E-02": [5, 26]}, part="CP-220", booking=("E-02", 5), search=("mechanical", 90)),
    Scenario("coolant_pump_late", "The pump takes 72 h to arrive, after the failure in 48 h: escalate.",
             failure_type="coolant_pump", tool_wear=None, slots={"E-02": [5, 26, 80]}, part="CP-220", order=None,
             search=("mechanical", 90), search_required=False),
    Scenario("spindle_bearing", "No part number in the manual: escalate, never invent one.",
             failure_type="spindle_bearing", tool_wear=None, stock={PART: 2}, slots={"E-01": [7], "E-02": [5]},
             part=None, search=None, search_required=False),
    Scenario("electrical_control", "No engineer has the skill and no part is listed: escalate.",
             failure_type="electrical_control", tool_wear=None, stock={PART: 2}, slots={"E-01": [7], "E-02": [5]},
             part=None, search=None, search_required=False),
    Scenario("failure_imminent", "Failure in 3 h, part in stock, but the first slot is in 7 h: escalate.",
             failure_in_h=3, tool_wear=(207, 3), stock={PART: 2}, slots={"E-01": [7, 30]}),
    Scenario("part_too_late", "Failure in 5 h, before even the fast supplier can deliver (6 h): escalate.",
             failure_in_h=5, tool_wear=(205, 4), slots={"E-01": [7, 30]}, order=None, search_required=False),
    Scenario("fast_supplier", "Failure in 16 h: the cheap supplier (24 h) is too late, so pay for the fast one (6 h).",
             failure_in_h=16, tool_wear=(200, 13), slots={"E-01": [7, 30]}, order=True, supplier=FAST,
             booking=("E-01", 7)),
    Scenario("detector_code", "The alert uses the AI4I code TWF instead of tool_wear: same plan as out_of_stock.",
             failure_type="TWF", stock={"EM-1006": 5, "CP-220": 1}, slots={"E-01": [7, 30]}, order=True,
             booking=("E-01", 30)),
    # Replans: the manager rejected part of the previous plan, with a reason.
    Scenario("replan_engineer_on_leave", "Replan: Priya is on leave, so book Wei Ling and keep the standing order.",
             slots={"E-01": [7, 30], "E-03": [33]}, order=True, booking=("E-03", 33),
             previous=Previous("Priya Nair is on leave on Thursday 15 Oct. Book someone else.", "pending_approval",
                               ("E-01", 30), avoid=("E-01",))),
    Scenario("replan_budget_frozen", "Replan: the order was rejected because purchasing is frozen: don't order, escalate.",
             slots={"E-01": [7, 30], "E-03": [33]}, search=None, search_required=False,
             previous=Previous("Purchasing is frozen until Monday: no new orders.", "rejected", ("E-01", 30))),
]}
CORE = ["out_of_stock", "in_stock", "no_engineer"]


@dataclass
class FleetMachine:
    machine_id: str
    failure_type: str = "tool_wear"
    failure_in_h: float = 48
    tool_wear: tuple[float, float] | None = (172, 38)

    def alert(self) -> Alert:
        wear, remaining = self.tool_wear or (None, None)
        return Alert(machine_id=self.machine_id, failure_type=self.failure_type, risk=0.93, tool_wear_min=wear,
                     remaining_cutting_min=remaining, detected_at=NOW,
                     predicted_failure_at=NOW + timedelta(hours=self.failure_in_h), readings=READINGS)


@dataclass
class Fleet:
    """Fleet mode: four machines alert at once and share one factory (the same engineers and parts store).
    A correct fleet: one tool-wear machine takes the only EM-1004 in stock and the earliest slot; the other two
    order it from the cheap supplier and get the two slots after delivery (one each); M-04's coolant pump
    can't arrive before its failure, so it escalates. No slot or part is promised twice."""

    about: str
    machines: list[FleetMachine]
    stock: dict[str, int]
    slots: dict[str, list[float]]
    from_stock: tuple[str, float]  # (engineer, slot hours after NOW) the stock machine gets
    after_delivery: list[tuple[str, float]]  # the slots the two ordering machines get, in any order
    escalates: tuple[str, ...]  # machines that must escalate


FLEET = Fleet(
    "Four machines, one EM-1004 in stock and shared engineers: one uses stock, two order, M-04 escalates.",
    machines=[FleetMachine("M-01", failure_in_h=48, tool_wear=(172, 38)),
              FleetMachine("M-02", failure_in_h=40, tool_wear=(180, 33)),
              FleetMachine("M-03", failure_in_h=44, tool_wear=(176, 36)),
              FleetMachine("M-04", failure_type="coolant_pump", failure_in_h=40, tool_wear=None)],
    stock={PART: 1, "EM-1006": 5},
    slots={"E-01": [7, 26], "E-03": [27], "E-02": [26]},
    from_stock=("E-01", 7),
    after_delivery=[("E-01", 26), ("E-03", 27)],
    escalates=("M-04",),
)


def seed() -> dict:
    """Mock factory seed data for every scenario (mocks/factory.py format)."""
    return {
        "note": "Test data pinned to NOW; see devtools/scenarios.py.",
        "parts": [
            {"part_number": number, "name": name, "location": "B-3",
             "stock": {**{s.name: s.stock.get(number, 0) for s in SCENARIOS.values()},
                       "fleet": FLEET.stock.get(number, 0)},
             "suppliers": [{"supplier": supplier, "unit_price": price, "currency": "EUR", "lead_time_hours": lead}
                           for supplier, price, lead in offers]}
            for number, (name, offers) in PARTS.items()
        ],
        "engineers": [{"engineer_id": eid, "name": name, "skills": skills} for eid, (name, skills) in ENGINEERS.items()],
        "slots": {**{s.name: [{"engineer_id": eid, "hours_after_reset": h} for eid, hours in s.slots.items()
                              for h in hours] for s in SCENARIOS.values()},
                  "fleet": [{"engineer_id": eid, "hours_after_reset": h} for eid, hours in FLEET.slots.items() for h in hours]},
    }
