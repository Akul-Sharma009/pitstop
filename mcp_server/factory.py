"""Client for the mock factory systems (inventory, supplier, calendar).

Only the endpoints the agent may use are wrapped here. Approve/reject endpoints are
deliberately missing: approval happens in the app, never through the agent.
See docs/contracts.md.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class FactoryClient:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url or os.environ.get("MOCKS_URL", "http://localhost:8000"),
            timeout=10.0,
            transport=transport,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """Return the JSON body, or {"error": ...} for client errors so the agent can react."""
        response = self._http.request(method, path, **kwargs)
        if 400 <= response.status_code < 500:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            return {"error": f"{response.status_code}: {detail}"}
        response.raise_for_status()
        return response.json()

    def check_inventory(self, part_number: str) -> dict[str, Any]:
        return self._call("GET", f"/inventory/parts/{part_number}")

    def get_supplier_quote(self, part_number: str) -> dict[str, Any]:
        return self._call("GET", f"/supplier/parts/{part_number}")

    def draft_purchase_order(self, part_number: str, quantity: int, machine_id: str, reason: str) -> dict[str, Any]:
        body = {"part_number": part_number, "quantity": quantity, "machine_id": machine_id, "reason": reason}
        return self._call("POST", "/supplier/purchase-orders", json=body)

    def find_free_engineers(
        self, skill: str, earliest_start: str, latest_end: str, duration_minutes: int
    ) -> dict[str, Any]:
        params = {
            "skill": skill,
            "earliest_start": earliest_start,
            "latest_end": latest_end,
            "duration_minutes": duration_minutes,
        }
        result = self._call("GET", "/calendar/engineers/free", params=params)
        return result if isinstance(result, dict) else {"slots": result}

    def draft_booking(
        self,
        engineer_id: str,
        machine_id: str,
        start: str,
        end: str,
        task: str,
        purchase_order_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "engineer_id": engineer_id,
            "machine_id": machine_id,
            "start": start,
            "end": end,
            "task": task,
            "purchase_order_id": purchase_order_id,
        }
        return self._call("POST", "/calendar/bookings", json=body)
