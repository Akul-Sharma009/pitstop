"""The manager approval screen and the operator summary (static page in app/static, by our teammate),
served together with a small API so the page needs one address and no CORS:

  GET  /api/latest                          latest alert and plan, from the agent service
  GET  /api/{purchase-orders|bookings}/{id}  an order or booking, from the mock factory
  POST /api/{purchase-orders|bookings}/{id}/{approve|reject}   the manager's decision, to the mock factory

This is the app side of the approval boundary: the agent and the MCP server have no route to approve or
reject (docs/contracts.md). Run: uvicorn app.server:app --port 8003
Env: AGENT_URL (default http://localhost:8002), MOCKS_URL (default http://localhost:8000).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
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
def decide(kind: str, item_id: str, action: str) -> JSONResponse:
    if action not in ("approve", "reject"):
        raise HTTPException(404, "unknown action")
    return relay(client("mocks").post(f"{_path(kind, item_id)}/{action}"))


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
