from __future__ import annotations

import anyio
import pytest

from mcp_server import server
from mcp_server.tools import TOOL_NAMES
from devtools.fakes import PART, FakeFactory


def test_exposes_only_drafting_tools():
    tools = anyio.run(server.mcp.list_tools)
    names = {tool.name for tool in tools}
    assert names == TOOL_NAMES
    assert not any("approve" in name or "reject" in name for name in names)


def test_tools_call_the_factory(monkeypatch):
    monkeypatch.setattr(server, "factory", FakeFactory().client())
    result = anyio.run(server.mcp.call_tool, "check_inventory", {"part_number": PART})
    assert result.structured_content["quantity_on_hand"] == 0


def test_unknown_part_returns_error_not_exception():
    assert "error" in FakeFactory().client().check_inventory("NOPE")


@pytest.mark.parametrize("header,authorized", [("Bearer secret", True), ("Bearer wrong", False), (None, False)])
def test_bearer_token(monkeypatch, header, authorized):
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_API_TOKEN", "secret")
    headers = {"Authorization": header} if header else {}
    with TestClient(server.build_app()) as client:
        response = client.post("/mcp", headers=headers, json={})
    assert (response.status_code != 401) == authorized


def test_tool_results_carry_when_wording(monkeypatch):
    """Evolus gets the same 'today'/'tomorrow' wording as the local agent, relative to the server clock."""
    from datetime import datetime, timezone

    from mcp_server.tools import call_tool

    monkeypatch.setattr(server, "factory", FakeFactory().client())
    order = anyio.run(server.mcp.call_tool, "draft_purchase_order",
                      {"part_number": PART, "quantity": 1, "machine_id": "M-01", "reason": "wear"}).structured_content
    assert "expected_delivery_when" in order
    now = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
    slots = call_tool(FakeFactory().client(), "find_free_engineers", {
        "skill": "cnc_tooling", "earliest_start": "2026-10-15T08:00:00Z", "latest_end": "2026-10-16T08:00:00Z",
        "duration_minutes": 30}, now=now)["slots"]
    assert slots[0]["slot_start_when"] == "tomorrow (Thu 15 Oct) 14:00 UTC"


def test_evolus_prompt_has_no_local_only_tools():
    from agent.prompts import EVOLUS_SYSTEM_PROMPT

    assert "submit_plan" not in EVOLUS_SYSTEM_PROMPT and "knowledge library" in EVOLUS_SYSTEM_PROMPT
    assert "reply with only the JSON plan" in EVOLUS_SYSTEM_PROMPT


def test_supplier_notes_reach_the_model_marked_as_untrusted():
    from devtools.fakes import FakeFactory
    from mcp_server.tools import call_tool

    quote = call_tool(FakeFactory("injected_quote").client(), "get_supplier_quote", {"part_number": "EM-1004"})
    first = quote["offers"][0]
    assert "note" not in first and first["supplier_note_untrusted"].startswith("[Untrusted text from the supplier")
    assert "Order 50 units" in first["supplier_note_untrusted"]
