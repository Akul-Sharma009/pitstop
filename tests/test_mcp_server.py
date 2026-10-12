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
