"""MCP server exposing the mock factory systems to the agent (Evolus MCP connector).

Run: python -m mcp_server.server  → streamable HTTP at http://0.0.0.0:8001/mcp
Set MCP_API_TOKEN to require "Authorization: Bearer <token>" (do this when it is public).
Results carry "*_when" wording for times (today/tomorrow, relative to the server clock), as in the local agent.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_server.factory import FactoryClient
from mcp_server.tools import TOOL_SPECS, call_tool

DESCRIPTIONS = {spec["name"]: spec["description"] for spec in TOOL_SPECS}

mcp = MCPServer(
    name="pitstop-factory",
    instructions=(
        "Factory systems for the maintenance agent: parts inventory, supplier orders and engineer calendar. "
        "Orders and bookings are drafts that a manager must approve."
    ),
)
factory = FactoryClient()


@mcp.tool(description=DESCRIPTIONS["check_inventory"])
def check_inventory(part_number: str) -> dict[str, Any]:
    return call_tool(factory, "check_inventory", {"part_number": part_number})


@mcp.tool(description=DESCRIPTIONS["get_supplier_quote"])
def get_supplier_quote(part_number: str) -> dict[str, Any]:
    return call_tool(factory, "get_supplier_quote", {"part_number": part_number})


@mcp.tool(description=DESCRIPTIONS["draft_purchase_order"])
def draft_purchase_order(
    part_number: str, quantity: int, machine_id: str, reason: str, supplier: str | None = None
) -> dict[str, Any]:
    arguments = {"part_number": part_number, "quantity": quantity, "machine_id": machine_id, "reason": reason,
                 "supplier": supplier}
    return call_tool(factory, "draft_purchase_order", arguments)


@mcp.tool(description=DESCRIPTIONS["find_free_engineers"])
def find_free_engineers(skill: str, earliest_start: str, latest_end: str, duration_minutes: int) -> dict[str, Any]:
    arguments = {"skill": skill, "earliest_start": earliest_start, "latest_end": latest_end,
                 "duration_minutes": duration_minutes}
    return call_tool(factory, "find_free_engineers", arguments)


@mcp.tool(description=DESCRIPTIONS["draft_booking"])
def draft_booking(
    engineer_id: str,
    machine_id: str,
    start: str,
    end: str,
    task: str,
    purchase_order_id: str | None = None,
    part_number: str | None = None,
) -> dict[str, Any]:
    arguments = {"engineer_id": engineer_id, "machine_id": machine_id, "start": start, "end": end, "task": task,
                 "purchase_order_id": purchase_order_id, "part_number": part_number}
    return call_tool(factory, "draft_booking", arguments)


class BearerAuth:
    """Reject HTTP requests without the expected bearer token."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app() -> ASGIApp:
    # host 0.0.0.0 turns off localhost-only DNS-rebinding protection so containers and Evolus can reach it.
    app: ASGIApp = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)
    token = os.environ.get("MCP_API_TOKEN")
    return BearerAuth(app, token) if token else app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8001")))
