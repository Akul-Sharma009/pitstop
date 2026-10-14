"""The manager approval screen and the operator summary (static page in app/static, by our teammate),
served together with a small API so the page needs one address and no CORS:

  GET  /api/latest                          latest alert and plan, from the agent service
  GET  /api/{purchase-orders|bookings}/{id}  an order or booking, from the mock factory
  POST /api/{purchase-orders|bookings}/{id}/{approve|reject}   the manager's decision, to the mock factory;
       a reject with {"reason": "..."} also asks the agent for a new plan with that reason (replan)

This is the app side of the approval boundary: the agent and the MCP server have no route to approve or
reject (docs/contracts.md). Run: uvicorn app.server:app --port 8003
Env: AGENT_URL (default http://localhost:8002), MOCKS_URL (default http://localhost:8000).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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


@app.get("/api/latest")
def latest() -> Any:
    try:
        response = client("agent").get("/incidents/latest")
    except httpx.HTTPError:
        raise HTTPException(503, "the agent service is not reachable")
    if response.status_code == 404:
        return {"alert": None, "result": None}
    return relay(response)


@app.get("/api/{kind}/{item_id}")
def get_item(kind: str, item_id: str) -> JSONResponse:
    return relay(client("mocks").get(_path(kind, item_id)))


@app.post("/api/{kind}/{item_id}/{action}")
def decide(kind: str, item_id: str, action: str, body: dict[str, Any] | None = Body(None)) -> JSONResponse:
    if action not in ("approve", "reject"):
        raise HTTPException(404, "unknown action")
    response = client("mocks").post(f"{_path(kind, item_id)}/{action}")
    reason = str((body or {}).get("reason") or "").strip()
    if action == "reject" and reason and response.status_code == 200:
        # The agent takes a while: replan in the background; the page follows it through /api/latest.
        threading.Thread(target=replan, args=(reason,), daemon=True).start()
    return relay(response)


def replan(reason: str) -> None:
    """Send the manager's reason and the previous plan's drafts, as the mocks have them now, to the agent."""
    result = client("agent").get("/incidents/latest").json().get("result") or {}

    def current(kind: str, item_id: str | None) -> dict[str, Any] | None:
        if not item_id:
            return None
        response = client("mocks").get(_path(kind, item_id))
        return response.json() if response.status_code == 200 else None

    body = {"feedback": reason, "purchase_order": current("purchase-orders", result.get("purchase_order_id")),
            "booking": current("bookings", result.get("booking_id"))}
    client("agent").post("/incidents/replan", json=body, timeout=600)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
