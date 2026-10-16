"""The manager approval screen and the operator summary (static page in app/static, by our teammate),
served together with a small API so the page needs one address and no CORS:

  GET  /api/latest                          latest alert and plan, from the agent service
  GET  /api/incidents[/{machine_id}]        every machine's latest alert and plan, or one machine's (fleet mode)
  GET  /api/{purchase-orders|bookings}/{id}  an order or booking, from the mock factory
  POST /api/{purchase-orders|bookings}/{id}/{approve|reject}   the manager's decision, to the mock factory;
       a reject with {"reason": "..."} also asks the agent for a new plan with that reason (replan)
  POST /api/purchase-orders/{id}/delay       demo control: the supplier delays the order; a pending booking that
       now starts before the part arrives is rejected and the agent replans (an approved one: the manager decides)
  GET  /api/progress/{machine_id}           while the agent plans: what it has done so far, from the mocks' call log
  POST /api/demo/start                      demo control: {"scenario": ...} resets the mock factory to a demo scenario,
       forgets every plan and sends fresh (simulated) alerts, so each judge can start from a clean slate
  POST /api/incidents/{machine_id}/worse     demo control: a simulated new reading moves the machine's predicted
       failure earlier; a pending booking that now ends after it is rejected and the agent replans for the new
       alert (an approved one: the manager decides); otherwise the agent keeps the plan if it still fits

This is the app side of the approval boundary: the agent and the MCP server have no route to approve or
reject (docs/contracts.md). Run: uvicorn app.server:app --port 8003
Env: AGENT_URL (default http://localhost:8002), MOCKS_URL (default http://localhost:8000),
SCREENS_PASSWORD (set it whenever the screens are reachable from the internet: the browser then asks for
SCREENS_USER, default "manager", and this password; the screens can approve orders).
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.times import readable, utc

STATIC = Path(__file__).with_name("static")
PATHS = {"purchase-orders": "/supplier/purchase-orders", "bookings": "/calendar/bookings"}

app = FastAPI(title="Pitstop screens")
clients: dict[str, httpx.Client] = {}


def client(name: str) -> httpx.Client:
    if name not in clients:
        default = {"agent": "http://localhost:8002", "mocks": "http://localhost:8000"}[name]
        clients[name] = httpx.Client(base_url=os.environ.get(f"{name.upper()}_URL", default), timeout=10.0)
    return clients[name]


def relay(response: httpx.Response) -> JSONResponse:
    try:
        body: Any = response.json()
    except ValueError:
        body = {"detail": response.text}
    return JSONResponse(body, status_code=response.status_code)


def _path(kind: str, item_id: str) -> str:
    if kind not in PATHS:
        raise HTTPException(404, "unknown kind")
    return f"{PATHS[kind]}/{item_id}"


@app.get("/api/status")
def status() -> dict[str, Any]:
    """For the screens' banner: is the agent service up, and does the AI model answer (GPU instance on)?"""
    try:
        health = client("agent").get("/health", timeout=8).json()
    except (httpx.HTTPError, ValueError):
        return {"agent": "offline", "model": "unknown"}
    return {"agent": "online", "model": health.get("model", "unknown"), "backend": health.get("backend")}


@app.get("/api/latest")
def latest() -> Any:
    try:
        response = client("agent").get("/incidents/latest")
    except httpx.HTTPError:
        raise HTTPException(503, "the agent service is not reachable")
    if response.status_code == 404:
        return {"alert": None, "result": None}
    return relay(response)


@app.get("/api/incidents")
def incidents() -> Any:
    """Fleet mode: every machine's latest alert and plan, sorted by machine ID."""
    try:
        return relay(client("agent").get("/incidents"))
    except httpx.HTTPError:
        raise HTTPException(503, "the agent service is not reachable")


@app.get("/api/incidents/{machine_id}")
def machine_incident(machine_id: str) -> Any:
    """Fleet mode: one machine's latest alert and plan (404 if it has none)."""
    try:
        return relay(client("agent").get(f"/incidents/{machine_id}"))
    except httpx.HTTPError:
        raise HTTPException(503, "the agent service is not reachable")


@app.get("/api/progress/{machine_id}")
def progress(machine_id: str) -> dict[str, Any]:
    """The steps of the machine's current plan so far, in plain words, from the factory's log of agent calls
    since the alert arrived (in Evolus the manual search happens in its knowledge library, so it isn't listed)."""
    incident = client("agent").get(f"/incidents/{machine_id}")
    if incident.status_code != 200:
        return {"steps": [], "next": None}
    since = utc(incident.json().get("received_at"))
    calls = client("mocks").get("/admin/calls").json().get("calls", [])
    mine = [c for c in calls if c.get("machine_id") == machine_id and since and utc(c.get("at")) and utc(c["at"]) >= since]
    names = {slot.get("engineer_id"): slot.get("name") for c in mine if c["tool"] == "find_free_engineers"
             for slot in (c.get("result") or {}).get("slots") or []}
    steps = [step_text(c, names) for c in mine]
    following = {"check_inventory": "Getting supplier quotes or finding an engineer…",
                 "get_supplier_quote": "Choosing a supplier and drafting the order…",
                 "draft_purchase_order": "Finding an engineer after the part arrives…",
                 "find_free_engineers": "Booking the engineer…", "draft_booking": "Checking the plan and writing it up…"}
    nxt = following.get(mine[-1]["tool"], "Working on the next step…") if mine else "Reading the alert and the manual…"
    return {"steps": steps, "next": nxt}


def step_text(call: dict[str, Any], names: dict[str, str] | None = None) -> str:
    """One agent tool call in plain words, for the planning screen."""
    tool, args, result = call["tool"], call.get("arguments") or {}, call.get("result") or {}
    part = args.get("part_number") or result.get("part_number") or "the part"
    if "error" in result:
        text = str(result["error"]).split(": ", 1)[-1]
        return {"check_inventory": f"Checked stock: {part} is not a known part",
                "draft_purchase_order": f"Order not placed: {text}",
                "find_free_engineers": f"Engineer search refused: {text}",
                "draft_booking": f"Slot not available: {text}"}.get(tool, f"{tool}: {text}")
    if tool == "check_inventory":
        return f"Checked stock: {result.get('quantity_on_hand', 0)} × {part} on hand"
    if tool == "get_supplier_quote":
        offers = result.get("offers") or []
        return f"Got {len(offers)} supplier quote{'s' * (len(offers) != 1)} for {part}"
    if tool == "draft_purchase_order":
        return (f"Drafted {result.get('po_id')}: {result.get('quantity', 1)} × {part} from {result.get('supplier')}, "
                f"arrives {readable(result.get('expected_delivery'))}")
    if tool == "find_free_engineers":
        slots = result.get("slots") or []
        return f"Searched for engineers: {len(slots)} free slot{'s' * (len(slots) != 1)} in the repair window"
    if tool == "draft_booking":
        engineer = (names or {}).get(result.get("engineer_id")) or result.get("engineer_id")
        return f"Drafted booking {result.get('booking_id')}: {engineer} at {readable(result.get('start'))}"
    return tool


@app.get("/api/{kind}/{item_id}")
def get_item(kind: str, item_id: str) -> JSONResponse:
    return relay(client("mocks").get(_path(kind, item_id)))


@app.post("/api/purchase-orders/{po_id}/delay")
def delay(po_id: str, body: dict[str, Any] | None = Body(None)) -> JSONResponse:
    """The supplier delays an order (demo control). Re-check the booking of the order's machine's plan against
    the new delivery."""
    response = client("mocks").post(f"{PATHS['purchase-orders']}/{po_id}/delay", params={"hours": (body or {}).get("hours")})
    if response.status_code != 200:
        return relay(response)
    order = response.json()
    result, machine = plan_of(order.get("machine_id"))
    booking = None
    if result.get("purchase_order_id") == po_id and result.get("booking_id"):
        found = client("mocks").get(_path("bookings", result["booking_id"]))
        booking = found.json() if found.status_code == 200 else None
    arrives = readable(order["expected_delivery"])
    reply = {"order": order, "booking": booking, "action": "none",
             "message": f"{po_id} now arrives {arrives}. The booked repair still starts after that, so the plan stands."}
    if not booking or booking["status"] == "rejected" or utc(booking["start"]) >= utc(order["expected_delivery"]):
        if not booking:
            reply["message"] = f"{po_id} now arrives {arrives}. No booking depends on it."
        return JSONResponse(reply)
    if booking["status"] == "approved":
        reply.update(action="manager_must_decide", message=(
            f"{po_id} now arrives {arrives}, after the approved repair {booking['booking_id']} "
            f"({readable(booking['start'])}). Reject the booking with a reason to get a new plan."))
        return JSONResponse(reply)
    rejected = client("mocks").post(f"{_path('bookings', booking['booking_id'])}/reject")
    reply["booking"] = rejected.json() if rejected.status_code == 200 else booking
    feedback = (f"The supplier delayed {po_id}: the part now arrives {arrives} instead of "
                f"{readable(order.get('original_delivery'))}. Booking {booking['booking_id']} started before that, "
                "so it was cancelled. Keep the order and book a repair after the new delivery, before the failure; "
                "if nothing fits, escalate.")
    threading.Thread(target=replan, args=(feedback, machine), daemon=True).start()
    reply.update(action="replanning", message=(
        f"{po_id} now arrives {arrives}, after the planned repair, so booking {booking['booking_id']} was cancelled. "
        "The agent is making a new plan."))
    return JSONResponse(reply)


@app.post("/api/incidents/{machine_id}/worse")
def worse(machine_id: str, body: dict[str, Any] | None = Body(None)) -> JSONResponse:
    """Failure comes sooner (demo control): a worse sensor reading: send a simulated alert for the machine with the failure predicted
    {"failure_in_h": N} hours from now (default: an hour before its booked repair starts)."""
    response = client("agent").get(f"/incidents/{machine_id}")
    if response.status_code != 200:
        return relay(response)
    incident = response.json()
    result = incident.get("result")
    if not result:
        raise HTTPException(409, "the agent is still planning")
    now = clock()
    hours = (body or {}).get("failure_in_h")
    if hours is None:
        start = utc(result.get("booking_start"))
        if not start:
            raise HTTPException(422, "no booked repair to come before: give failure_in_h")
        hours = max((start - now).total_seconds() / 3600 - 1, 1)
    if float(hours) <= 0:
        raise HTTPException(422, "failure_in_h must be positive")
    alert = worse_alert(incident["alert"], now, now + timedelta(hours=float(hours)))
    failure = utc(alert["predicted_failure_at"])
    booking = None
    if result.get("booking_id"):
        found = client("mocks").get(_path("bookings", result["booking_id"]))
        booking = found.json() if found.status_code == 200 else None
    reply = {"alert": alert, "booking": booking}
    if booking and booking["status"] == "pending_approval" and utc(booking["end"]) > failure:
        rejected = client("mocks").post(f"{_path('bookings', booking['booking_id'])}/reject")
        reply["booking"] = rejected.json() if rejected.status_code == 200 else booking
        order = result.get("purchase_order_id")
        feedback = (f"A new sensor reading moved the predicted failure to {readable(failure)}, before booking "
                    f"{booking['booking_id']} ({readable(booking['start'])}) would finish, so it was cancelled. "
                    + (f"Keep {order} and book a repair after it arrives, before the new failure; "
                       if order else "Book an earlier repair, before the new failure; ")
                    + "if nothing fits, escalate.")
        threading.Thread(target=replan, args=(feedback, machine_id, alert), daemon=True).start()
        reply.update(action="replanning", message=(
            f"{machine_id} is now predicted to fail {readable(failure)}, before booking {booking['booking_id']} "
            "would finish, so it was cancelled. The agent is making a new plan."))
        return JSONResponse(reply)
    # Like any alert: the agent keeps an open plan (with a warning if an approved repair is now too late).
    threading.Thread(target=send_alert, args=(alert,), daemon=True).start()
    if booking and booking["status"] == "approved" and utc(booking["end"]) > failure:
        reply.update(action="manager_must_decide", message=(
            f"{machine_id} is now predicted to fail {readable(failure)}, before the approved repair "
            f"{booking['booking_id']} ({readable(booking['start'])}). Reject the booking with a reason to get a new plan."))
    else:
        reply.update(action="alert_sent", message=(
            f"{machine_id} is now predicted to fail {readable(failure)}. The alert went to the agent: a plan that "
            "still fits is kept."))
    return JSONResponse(reply)


# Demo scenarios a judge can start (mocks/seed.json), and the machines that alert in each.
DEMO_SCENARIOS = {
    "out_of_stock": "Part out of stock: order it, book an engineer after delivery",
    "in_stock": "Part in stock: book the earliest engineer",
    "no_engineer": "No engineer free in time: order the part, escalate",
    "injection": "A supplier quote hides instructions to the agent: ignore them",
    "fleet": "Fleet: four machines share engineers and one spare cutter",
}
READINGS = {"air_temperature_k": 298.9, "process_temperature_k": 309.1, "rotational_speed_rpm": 1420.0, "torque_nm": 46.3}


def demo_alerts(scenario: str, now: datetime) -> list[dict[str, Any]]:
    """Alerts like the detector replay's (docs/contracts.md), marked simulated: they come from a button, not
    the sensor replay. Tool wear: the replay's typical alert (≈40 cutting minutes left); the fleet's values
    are the real replay's (M-01–M-03) plus its simulated coolant pump (M-04)."""
    detected = now.replace(second=0, microsecond=0)
    per_day = float(os.environ.get("CUTTING_MIN_PER_DAY", 20))

    def wear(machine: str, worn: float, left: float) -> dict[str, Any]:
        return {"machine_id": machine, "failure_type": "tool_wear", "risk": 0.9, "simulated": True,
                "tool_wear_min": worn, "remaining_cutting_min": left, "detected_at": detected.isoformat(),
                "predicted_failure_at": (detected + timedelta(days=left / per_day)).isoformat(), "readings": READINGS}

    if scenario != "fleet":
        return [wear("M-01", 160, 40)]
    pump = {"machine_id": "M-04", "failure_type": "coolant_pump", "risk": 0.8, "simulated": True,
            "tool_wear_min": None, "remaining_cutting_min": None, "detected_at": detected.isoformat(),
            "predicted_failure_at": (detected + timedelta(hours=40)).isoformat(), "readings": {}}
    return [wear("M-01", 158, 42), wear("M-02", 158, 42), wear("M-03", 157, 43), pump]


@app.post("/api/demo/start")
def start_demo(body: dict[str, Any] | None = Body(None)) -> JSONResponse:
    """A clean slate (demo control): reset the mock factory to a scenario, forget every plan, send fresh alerts."""
    scenario = (body or {}).get("scenario") or "out_of_stock"
    if scenario not in DEMO_SCENARIOS:
        raise HTTPException(422, f"unknown scenario; use one of {', '.join(DEMO_SCENARIOS)}")
    try:
        planning = [i["alert"]["machine_id"] for i in client("agent").get("/incidents").json()["incidents"]
                    if i.get("result") is None]
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(503, "the agent service is not reachable")
    if planning:
        raise HTTPException(409, f"the agent is still planning for {', '.join(planning)}: wait for it, then start again")
    reset = client("mocks").post("/admin/reset", params={"scenario": scenario})
    if reset.status_code != 200:
        return relay(reset)
    client("agent").delete("/incidents")
    alerts = demo_alerts(scenario, clock())
    for alert in alerts:  # the agent takes a minute or two each; the page follows the plans
        threading.Thread(target=send_alert, args=(alert,), daemon=True).start()
    machines = [a["machine_id"] for a in alerts]
    return JSONResponse({"scenario": scenario, "machines": machines, "message": (
        f"Fresh demo: {DEMO_SCENARIOS[scenario]}. The factory was reset and {', '.join(machines)} sent an alert; "
        "the agent is planning (usually 1–3 minutes" + (" per machine, one at a time" if len(machines) > 1 else "") + ").")})


def clock() -> datetime:
    return datetime.now(timezone.utc)


def send_alert(alert: dict[str, Any]) -> None:
    client("agent").post("/incidents", json=alert, timeout=600)


def worse_alert(alert: dict[str, Any], now: datetime, failure: datetime) -> dict[str, Any]:
    """The machine's alert, detected now with the failure at `failure`; tool wear moved to match. Simulated."""
    new = {**alert, "detected_at": now.isoformat(), "predicted_failure_at": failure.isoformat(), "simulated": True}
    wear, left = alert.get("tool_wear_min"), alert.get("remaining_cutting_min")
    before = (utc(alert["predicted_failure_at"]) - utc(alert["detected_at"])).total_seconds()
    if wear is not None and left is not None and before > 0:
        remaining = left * (failure - now).total_seconds() / before  # same cutting minutes per day
        new.update(remaining_cutting_min=round(remaining, 1), tool_wear_min=round(wear + left - remaining, 1))
    return new


def plan_of(machine_id: str | None) -> tuple[dict[str, Any], str | None]:
    """(that machine's latest plan, the machine); without a machine, or if it has no alert: the latest alert's."""
    if machine_id:
        response = client("agent").get(f"/incidents/{machine_id}")
        if response.status_code == 200:
            return response.json().get("result") or {}, machine_id
    response = client("agent").get("/incidents/latest")
    return (response.json().get("result") or {}) if response.status_code == 200 else {}, None


@app.post("/api/{kind}/{item_id}/{action}")
def decide(kind: str, item_id: str, action: str, body: dict[str, Any] | None = Body(None)) -> JSONResponse:
    if action not in ("approve", "reject"):
        raise HTTPException(404, "unknown action")
    response = client("mocks").post(f"{_path(kind, item_id)}/{action}")
    reason = str((body or {}).get("reason") or "").strip()
    if action == "reject" and reason and response.status_code == 200:
        # The agent takes a while: replan in the background; the page follows it through /api/latest.
        machine = response.json().get("machine_id")  # whose plan it was (fleet mode)
        threading.Thread(target=replan, args=(reason, machine), daemon=True).start()
    return relay(response)


def replan(reason: str, machine_id: str | None = None, alert: dict[str, Any] | None = None) -> None:
    """Send the manager's reason and the previous plan's drafts, as the mocks have them now, to the agent
    (and a newer alert for the machine, after a worse sensor reading)."""
    result, machine_id = plan_of(machine_id)

    def current(kind: str, item_id: str | None) -> dict[str, Any] | None:
        if not item_id:
            return None
        response = client("mocks").get(_path(kind, item_id))
        return response.json() if response.status_code == 200 else None

    body = {"feedback": reason, "machine_id": machine_id,
            "purchase_order": current("purchase-orders", result.get("purchase_order_id")),
            "booking": current("bookings", result.get("booking_id")), **({"alert": alert} if alert else {})}
    client("agent").post("/incidents/replan", json=body, timeout=600)


@app.middleware("http")
async def password(request, call_next):
    """HTTP basic auth on everything when SCREENS_PASSWORD is set (the browser shows its own login box)."""
    expected = os.environ.get("SCREENS_PASSWORD")
    if expected:
        user = os.environ.get("SCREENS_USER", "manager")
        token = base64.b64encode(f"{user}:{expected}".encode()).decode()
        given = request.headers.get("authorization", "")
        if not secrets.compare_digest(given.encode(), f"Basic {token}".encode()):
            return JSONResponse({"detail": "login required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Pitstop", charset="UTF-8"'})
    return await call_next(request)


@app.middleware("http")
async def revalidate(request, call_next):
    """Browsers check for a newer page on every load (ETag), so a rebuild shows up without a hard reload."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
