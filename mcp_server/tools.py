"""The agent's factory tools: one definition shared by the MCP server and the local agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.times import label_times
from mcp_server.factory import FactoryClient

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "check_inventory",
        "description": "Look up a spare part in the factory parts store. Returns quantity_on_hand and location.",
        "parameters": {
            "type": "object",
            "properties": {"part_number": {"type": "string", "description": "Part number from the machine manual."}},
            "required": ["part_number"],
        },
    },
    {
        "name": "get_supplier_quote",
        "description": "Get the supplier's price and lead time (hours) for a part.",
        "parameters": {
            "type": "object",
            "properties": {"part_number": {"type": "string"}},
            "required": ["part_number"],
        },
    },
    {
        "name": "draft_purchase_order",
        "description": (
            "Draft a purchase order for a missing part. The order is created as pending_approval: "
            "a manager must approve it. Returns po_id and expected_delivery."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_number": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "machine_id": {"type": "string"},
                "reason": {"type": "string", "description": "One sentence for the manager."},
            },
            "required": ["part_number", "quantity", "machine_id", "reason"],
        },
    },
    {
        "name": "find_free_engineers",
        "description": (
            "Find engineers with a given skill who have a free slot of duration_minutes fully inside "
            "[earliest_start, latest_end]. Times are ISO 8601 with time zone. Returns {slots: [...]}, maybe empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Use cnc_tooling for tool-wear repairs."},
                "earliest_start": {"type": "string"},
                "latest_end": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 1},
            },
            "required": ["skill", "earliest_start", "latest_end", "duration_minutes"],
        },
    },
    {
        "name": "draft_booking",
        "description": (
            "Draft a booking of an engineer's free slot for the repair. Created as pending_approval: "
            "a manager must approve it. Returns booking_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "engineer_id": {"type": "string"},
                "machine_id": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "task": {"type": "string"},
                "purchase_order_id": {"type": ["string", "null"]},
            },
            "required": ["engineer_id", "machine_id", "start", "end", "task"],
        },
    },
]

TOOL_NAMES = {spec["name"] for spec in TOOL_SPECS}


def call_tool(client: FactoryClient, name: str, arguments: dict[str, Any], now: datetime | None = None) -> Any:
    """Run one tool. Times in the result get 'today'/'tomorrow' wording relative to `now` (default: the clock)."""
    if name not in TOOL_NAMES:
        return {"error": f"unknown tool {name}"}
    try:
        result = getattr(client, name)(**arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    return label_times(result, now or datetime.now(timezone.utc))
