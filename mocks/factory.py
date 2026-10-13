"""Mock factory systems (inventory, supplier, engineer calendar) implementing docs/contracts.md section 3.

In-memory and seeded from JSON (`mocks/seed.json` for the demo). Times are relative to the last reset:
`/admin/reset?scenario=` restarts the scenario with engineer slots at fixed hours after that moment, so reset
just before a demo run. The same class backs the tests and the scenario eval (`devtools/fakes.py`, pinned clock).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

SEED = Path(__file__).with_name("seed.json")
Clock = Callable[[], datetime]


def load_seed(path: str | Path = SEED) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _utc(value: str) -> datetime:
    """Accept Z or an offset; a time without a zone is UTC (the LLM sometimes drops it)."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_hour(moment: datetime) -> datetime:
    """Round up to the whole hour: suppliers quote deliveries on the hour."""
    whole = moment.replace(minute=0, second=0, microsecond=0)
    return whole if whole == moment else whole + timedelta(hours=1)


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})


class MockFactory:
    """One factory in one demo scenario: out_of_stock, in_stock or no_engineer."""

    def __init__(self, seed: dict[str, Any], scenario: str = "out_of_stock", clock: Clock = _now):
        self.seed = seed
        self.clock = clock
        self.parts = {p["part_number"]: p for p in seed["parts"]}
        self.engineers = {e["engineer_id"]: e for e in seed["engineers"]}
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., httpx.Response]]] = [
            ("POST", re.compile(r"/admin/reset"), self._reset),
            ("GET", re.compile(r"/health"), self._health),
            ("GET", re.compile(r"/inventory/parts/([^/]+)"), self._inventory),
            ("GET", re.compile(r"/supplier/parts/([^/]+)"), self._quote),
            ("POST", re.compile(r"/supplier/purchase-orders"), self._draft_order),
            ("GET", re.compile(r"/supplier/purchase-orders/([^/]+)"), self._get_order),
            ("POST", re.compile(r"/supplier/purchase-orders/([^/]+)/(approve|reject)"), self._decide_order),
            ("GET", re.compile(r"/calendar/engineers/free"), self._free),
            ("POST", re.compile(r"/calendar/bookings"), self._draft_booking),
            ("GET", re.compile(r"/calendar/bookings/([^/]+)"), self._get_booking),
            ("POST", re.compile(r"/calendar/bookings/([^/]+)/(approve|reject)"), self._decide_booking),
        ]
        self.reset(scenario)

    def reset(self, scenario: str) -> None:
        if scenario not in self.seed["slots"]:
            raise ValueError(f"unknown scenario {scenario!r}; use one of {', '.join(self.seed['slots'])}")
        self.scenario = scenario
        self.started = self.clock().replace(minute=0, second=0, microsecond=0)  # slots on whole hours
        self.stock = {number: part["stock"][scenario] for number, part in self.parts.items()}
        self.slots = [
            {"engineer_id": s["engineer_id"], "start": self.started + timedelta(hours=s["hours_after_reset"]),
             "minutes": s.get("minutes", 120)}
            for s in self.seed["slots"][scenario]
        ]
        self.orders: dict[str, dict[str, Any]] = {}
        self.bookings: dict[str, dict[str, Any]] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        for method, pattern, handler in self.routes:
            match = pattern.fullmatch(request.url.path)
            if match and request.method == method:
                return handler(request, *match.groups())
        return _error(404, "not found")

    # --- demo control ---------------------------------------------------------------------------

    def _reset(self, request: httpx.Request) -> httpx.Response:
        try:
            self.reset(request.url.params.get("scenario", "out_of_stock"))
        except ValueError as exc:
            return _error(400, str(exc))
        return httpx.Response(200, json={"scenario": self.scenario, "started": self.started.isoformat()})

    def _health(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scenario": self.scenario, "note": self.seed.get("note", "")})

    # --- inventory and supplier -----------------------------------------------------------------

    def _inventory(self, request: httpx.Request, number: str) -> httpx.Response:
        part = self.parts.get(number)
        if not part:
            return _error(404, "unknown part")
        return httpx.Response(200, json={"part_number": number, "name": part["name"],
                                         "quantity_on_hand": self.stock[number], "location": part["location"]})

    def _quote(self, request: httpx.Request, number: str) -> httpx.Response:
        supplier = (self.parts.get(number) or {}).get("supplier")
        if not supplier:
            return _error(404, "supplier does not sell this part")
        return httpx.Response(200, json={"part_number": number, **supplier})

    def _draft_order(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        supplier = (self.parts.get(body.get("part_number")) or {}).get("supplier")
        if not supplier:  # an invented part number can never be ordered
            return _error(404, "supplier does not sell this part")
        quantity = int(body.get("quantity") or 1)
        po_id = f"PO-{len(self.orders) + 1:04d}"
        order = {
            **body, "po_id": po_id, "status": "pending_approval", "quantity": quantity,
            "supplier": supplier["supplier"],
            "expected_delivery": _next_hour(self.clock() + timedelta(hours=supplier["lead_time_hours"])).isoformat(),
            "total_price": round(supplier["unit_price"] * quantity, 2), "currency": supplier["currency"],
        }
        self.orders[po_id] = order
        return httpx.Response(200, json=order)

    def _get_order(self, request: httpx.Request, po_id: str) -> httpx.Response:
        order = self.orders.get(po_id)
        return httpx.Response(200, json=order) if order else _error(404, "unknown purchase order")

    def _decide_order(self, request: httpx.Request, po_id: str, decision: str) -> httpx.Response:
        """App only: never exposed to the agent or the MCP server."""
        order = self.orders.get(po_id)
        if not order:
            return _error(404, "unknown purchase order")
        if order["status"] != "pending_approval":
            return _error(409, f"order is already {order['status']}")
        order["status"] = "approved" if decision == "approve" else "rejected"
        return httpx.Response(200, json=order)

    # --- engineer calendar ----------------------------------------------------------------------

    def _free(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        try:
            earliest, latest = _utc(params["earliest_start"]), _utc(params["latest_end"])
            duration = timedelta(minutes=int(params["duration_minutes"]))
        except (KeyError, ValueError) as exc:
            return _error(422, f"bad query: {exc}")
        skill = params.get("skill", "")
        free = []
        for slot in self.slots:
            engineer = self.engineers[slot["engineer_id"]]
            fits = slot["start"] >= earliest and slot["start"] + duration <= latest
            if skill in engineer["skills"] and fits and duration <= timedelta(minutes=slot["minutes"]):
                free.append({"engineer_id": engineer["engineer_id"], "name": engineer["name"], "skills": engineer["skills"],
                             "slot_start": slot["start"].isoformat(), "slot_end": (slot["start"] + duration).isoformat()})
        return httpx.Response(200, json=sorted(free, key=lambda s: s["slot_start"]))

    def _draft_booking(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        try:
            start = _utc(body["start"])
        except (KeyError, ValueError) as exc:
            return _error(422, f"bad booking: {exc}")
        if not any(s["engineer_id"] == body.get("engineer_id") and s["start"] == start for s in self.slots):
            return _error(409, "engineer is not free at that time")
        order = self.orders.get(body.get("purchase_order_id") or "")
        if order and start < _utc(order["expected_delivery"]):
            return _error(409, f"the part for {order['po_id']} arrives {order['expected_delivery']}")
        booking_id = f"BK-{len(self.bookings) + 1:04d}"
        booking = {**body, "booking_id": booking_id, "status": "pending_approval"}
        self.bookings[booking_id] = booking
        return httpx.Response(200, json=booking)

    def _get_booking(self, request: httpx.Request, booking_id: str) -> httpx.Response:
        booking = self.bookings.get(booking_id)
        return httpx.Response(200, json=booking) if booking else _error(404, "unknown booking")

    def _decide_booking(self, request: httpx.Request, booking_id: str, decision: str) -> httpx.Response:
        """App only. Approving takes the slot off the free list."""
        booking = self.bookings.get(booking_id)
        if not booking:
            return _error(404, "unknown booking")
        if booking["status"] != "pending_approval":
            return _error(409, f"booking is already {booking['status']}")
        booking["status"] = "approved" if decision == "approve" else "rejected"
        if decision == "approve":
            start = _utc(booking["start"])
            self.slots = [s for s in self.slots if not (s["engineer_id"] == booking["engineer_id"] and s["start"] == start)]
        return httpx.Response(200, json=booking)
