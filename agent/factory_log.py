"""Read the mock factory's log of agent endpoint calls (GET /admin/calls, app/demo only).

An Evolus chat reply carries only the final text, not the tools the agent called. The calls still land in
our mock factory, so we read them back from there as the run's trace.
"""

from __future__ import annotations

import os

import httpx

from agent.models import ToolCall


class FactoryLog:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url or os.environ.get("MOCKS_URL", "http://localhost:8000"), timeout=10.0, transport=transport
        )

    def _get(self, after: int) -> dict:
        response = self._http.get("/admin/calls", params={"after": after})
        response.raise_for_status()
        return response.json()

    def focus(self, machine_id: str | None) -> None:
        """Tell the factory which machine the next calls are for (MCP calls carry no machine of their own)."""
        self._http.post("/admin/focus", params={"machine_id": machine_id or ""}).raise_for_status()

    def last(self) -> int:
        """The sequence number of the latest call so far."""
        return int(self._get(0)["last"])

    def calls_after(self, seq: int) -> list[ToolCall]:
        return [ToolCall(tool=c["tool"], arguments=c["arguments"], result=c["result"]) for c in self._get(seq)["calls"]]
