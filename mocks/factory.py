"""Mock factory systems (inventory, supplier, engineer calendar) implementing docs/contracts.md section 3.

In-memory and seeded from JSON (`mocks/seed.json` for the demo). Times are relative to the last reset:
`/admin/reset?scenario=` restarts the scenario with engineer slots at fixed hours after that moment, so reset
just before a demo run. The same class backs the tests and the scenario eval (`devtools/fakes.py`, pinned clock).

Every call to an agent endpoint is logged as a tool call (`GET /admin/calls`), so a run in Evolus, whose
reply has no tool calls, can still be shown and checked like a local run.

Fleet mode: several machines share the engineers and the parts store. A call's machine is its `X-Machine-Id`
header (local agent), or else the admin focus (`POST /admin/focus?machine_id=`), which the Evolus backend sets
before each of its queued runs. A drafted booking holds its slot and reserves its part from stock until the
manager rejects it, so two machines can't plan with the same engineer slot or the same last part.
"""

from __future__ import annotations

import json
import re
import threading
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
        # Offers per part, cheapest first; a seed part may give one "supplier" or a list of "suppliers".
        self.offers = {number: sorted(p.get("suppliers") or ([p["supplier"]] if p.get("supplier") else []),
                                      key=lambda o: o["unit_price"]) for number, p in self.parts.items()}
        self.engineers = {e["engineer_id"]: e for e in seed["engineers"]}
        self.calls: list[dict[str, Any]] = []
        self.seq = 0  # keeps counting across resets, so "calls after N" stays meaningful
        self.routes: list[tuple[str, re.Pattern[str], Callable[..., httpx.Response]]] = [
            ("POST", re.compile(r"/admin/reset"), self._reset),
            ("POST", re.compile(r"/admin/focus"), self._focus),
            ("GET", re.compile(r"/health"), self._health),
            ("GET", re.compile(r"/admin/calls"), self._calls),
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
        self.lock = threading.Lock()  # the fleet eval runs several agents on one factory in threads
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
        self.calls = []  # the sequence number keeps counting
        self.bookings: dict[str, dict[str, Any]] = {}
        self.focus: str | None = None  # the machine the Evolus backend is planning for (POST /admin/focus)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            for method, pattern, handler in self.routes:
                match = pattern.fullmatch(request.url.path)
                if match and request.method == method:
                    response = handler(request, *match.groups())
                    self._log(request, match.groups(), response)
                    return response
        return _error(404, "not found")

    def _machine(self, request: httpx.Request) -> str | None:
        """The machine a call is for: the X-Machine-Id header, else the admin focus (None: not known)."""
        return request.headers.get("x-machine-id") or self.focus

    def _active(self) -> list[dict[str, Any]]:
        """Bookings that hold their slot and part: every one the manager has not rejected."""
        return [b for b in self.bookings.values() if b["status"] != "rejected"]

    def _holder(self, engineer_id: str, start: datetime) -> dict[str, Any] | None:
        return next((b for b in self._active() if b.get("engineer_id") == engineer_id and _utc(b["start"]) == start), None)

    def _available(self, number: str) -> int:
        """Stock on hand minus the units drafted bookings have reserved."""
        reserved = sum(1 for b in self._active() if b.get("reserved_part") == number)
        return max(self.stock.get(number, 0) - reserved, 0)

    def _log(self, request: httpx.Request, groups: tuple[str, ...], response: httpx.Response) -> None:
        """Record agent endpoint calls in the shape of the MCP tools' results (mcp_server/factory.py)."""
        path, method = request.url.path, request.method
        if method == "GET" and path.startswith("/inventory/parts/"):
            tool, arguments = "check_inventory", {"part_number": groups[0]}
        elif method == "GET" and path.startswith("/supplier/parts/"):
            tool, arguments = "get_supplier_quote", {"part_number": groups[0]}
        elif method == "POST" and path == "/supplier/purchase-orders":
            tool, arguments = "draft_purchase_order", json.loads(request.content or b"{}")
        elif method == "GET" and path == "/calendar/engineers/free":
            tool, arguments = "find_free_engineers", dict(request.url.params)
            if arguments.get("duration_minutes", "").isdigit():
                arguments["duration_minutes"] = int(arguments["duration_minutes"])
        elif method == "POST" and path == "/calendar/bookings":
            tool, arguments = "draft_booking", json.loads(request.content or b"{}")
        else:
            return
        body = response.json()
        if response.status_code >= 400:
            result: Any = {"error": f"{response.status_code}: {body.get('detail', '')}"}
        else:
            result = {"slots": body} if tool == "find_free_engineers" else body
        self.seq += 1
        self.calls.append({"seq": self.seq, "tool": tool, "arguments": arguments, "result": result,
                           "machine_id": self._machine(request)})

    # --- demo control ---------------------------------------------------------------------------

    def _reset(self, request: httpx.Request) -> httpx.Response:
        try:
            self.reset(request.url.params.get("scenario", "out_of_stock"))
        except ValueError as exc:
            return _error(400, str(exc))
        return httpx.Response(200, json={"scenario": self.scenario, "started": self.started.isoformat()})

    def _focus(self, request: httpx.Request) -> httpx.Response:
        """Demo/Evolus backend only: the machine that calls without an X-Machine-Id header are for."""
        self.focus = request.url.params.get("machine_id") or None
        return httpx.Response(200, json={"focus": self.focus})

    def _calls(self, request: httpx.Request) -> httpx.Response:
        """App/demo only: the agent endpoint calls after sequence number `after` (default 0)."""
        try:
            after = int(request.url.params.get("after", 0))
        except ValueError:
            return _error(422, "after must be an integer")
        return httpx.Response(200, json={"last": self.seq, "calls": [c for c in self.calls if c["seq"] > after]})

    def _health(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scenario": self.scenario, "note": self.seed.get("note", "")})

    # --- inventory and supplier -----------------------------------------------------------------

    def _inventory(self, request: httpx.Request, number: str) -> httpx.Response:
        part = self.parts.get(number)
        if not part:
            return _error(404, "unknown part")
        return httpx.Response(200, json={"part_number": number, "name": part["name"],
                                         "quantity_on_hand": self._available(number), "location": part["location"]})

    def _quote(self, request: httpx.Request, number: str) -> httpx.Response:
        offers = self.offers.get(number)
        if not offers:
            return _error(404, "no supplier sells this part")
        now = self.clock()
        return httpx.Response(200, json={"part_number": number, "offers": [
            {**offer, "expected_delivery": _next_hour(now + timedelta(hours=offer["lead_time_hours"])).isoformat()}
            for offer in offers]})

    def _draft_order(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        offers = self.offers.get(body.get("part_number"))
        if not offers:  # an invented part number can never be ordered
            return _error(404, "no supplier sells this part")
        wanted = body.get("supplier")
        supplier = next((o for o in offers if o["supplier"] == wanted), None) if wanted else offers[0]
        if not supplier:
            return _error(404, f"{wanted} does not sell {body.get('part_number')}; "
                               f"offers: {', '.join(o['supplier'] for o in offers)}")
        open_order = next((o for o in self.orders.values() if o["part_number"] == body.get("part_number")
                           and o.get("machine_id") == body.get("machine_id") and o["status"] != "rejected"), None)
        if open_order:  # purchasing rule: one open order per part and machine
            return _error(409, f"{open_order['po_id']} for {open_order['part_number']} is already on order "
                               f"({open_order['status'].replace('_', ' ')}), expected delivery "
                               f"{open_order['expected_delivery']}: use it, do not order again")
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
        if earliest >= latest:
            return _error(422, f"the window is empty: earliest_start {params['earliest_start']} is not before latest_end "
                               f"{params['latest_end']}. If the part arrives after the predicted failure, order from a "
                               "faster supplier, or escalate to the manager")
        # Demo rule, the same one the local agent enforces: once a part is on order, the repair window starts
        # when it arrives. Here it holds for every agent, Evolus included. In a fleet, only the requesting
        # machine's own orders count (another machine's order says nothing about this repair).
        machine = self._machine(request)
        ordered = [o for o in self.orders.values() if o["status"] != "rejected"
                   and (machine is None or o.get("machine_id") == machine)]
        if ordered:
            last = max(ordered, key=lambda o: _utc(o["expected_delivery"]))
            if earliest < _utc(last["expected_delivery"]):
                return _error(409, f"the part for {last['po_id']} arrives {last['expected_delivery']}: "
                                   f"search with earliest_start {last['expected_delivery']}")
        skill = params.get("skill", "")
        free = []
        for slot in self.slots:
            engineer = self.engineers[slot["engineer_id"]]
            fits = slot["start"] >= earliest and slot["start"] + duration <= latest
            held = self._holder(slot["engineer_id"], slot["start"])  # drafted for a repair (this or another machine)
            if skill in engineer["skills"] and fits and not held and duration <= timedelta(minutes=slot["minutes"]):
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
        if holder := self._holder(body.get("engineer_id"), start):
            return _error(409, f"that slot is already held by {holder['booking_id']} ({holder.get('machine_id')}): "
                               "search again for a free slot")
        order = self.orders.get(body.get("purchase_order_id") or "")
        if order and start < _utc(order["expected_delivery"]):
            return _error(409, f"the part for {order['po_id']} arrives {order['expected_delivery']}")
        reserved = None
        if not order:  # the part comes from stock: the booking reserves one unit
            reserved = body.get("part_number") or self._last_found_in_stock(request)
            if reserved and self._available(reserved) < 1:
                holders = [f"{b['booking_id']} ({b.get('machine_id')})" for b in self._active()
                           if b.get("reserved_part") == reserved]
                return _error(409, f"no {reserved} left in stock"
                                   + (f" (reserved for {', '.join(holders)})" if holders else "")
                                   + f": order {reserved}, then book after it arrives")
        booking_id = f"BK-{len(self.bookings) + 1:04d}"
        booking = {**body, "booking_id": booking_id, "status": "pending_approval"}
        if reserved:
            booking["reserved_part"] = reserved
        self.bookings[booking_id] = booking
        return httpx.Response(200, json=booking)

    def _last_found_in_stock(self, request: httpx.Request) -> str | None:
        """For a booking without part_number (e.g. from Evolus): the part this machine last found in stock."""
        machine = self._machine(request)
        if machine is None:
            return None
        for call in reversed(self.calls):
            result = call["result"]
            if call["tool"] == "check_inventory" and call.get("machine_id") == machine and "error" not in result:
                return result["part_number"] if result.get("quantity_on_hand", 0) > 0 else None
        return None

    def _get_booking(self, request: httpx.Request, booking_id: str) -> httpx.Response:
        booking = self.bookings.get(booking_id)
        return httpx.Response(200, json=booking) if booking else _error(404, "unknown booking")

    def _decide_booking(self, request: httpx.Request, booking_id: str, decision: str) -> httpx.Response:
        """App only."""
        booking = self.bookings.get(booking_id)
        if not booking:
            return _error(404, "unknown booking")
        if booking["status"] != "pending_approval":
            return _error(409, f"booking is already {booking['status']}")
        booking["status"] = "approved" if decision == "approve" else "rejected"
        # A pending or approved booking holds its slot and part; a rejected one gives both back.
        return httpx.Response(200, json=booking)
