# Contracts between the parts

These are the interfaces the parts agree on. Change this file first, then both sides.
All times are ISO 8601 with time zone (e.g. `2026-10-14T09:00:00+00:00`).

```
detector ──Alert──▶ app ──Alert──▶ agent service ──tools──▶ mocks (inventory, supplier, calendar)
                     ▲                  │                      ▲
                     └──── AgentResult ─┘        app approves ─┘
```

## 1. Alert (detector → app → agent service)

`POST {AGENT_URL}/incidents` with:

```json
{
  "machine_id": "M-01",
  "failure_type": "tool_wear",
  "risk": 0.93,
  "tool_wear_min": 172,
  "remaining_cutting_min": 38,
  "detected_at": "2026-10-14T08:00:00+00:00",
  "predicted_failure_at": "2026-10-16T08:00:00+00:00",
  "readings": {
    "air_temperature_k": 298.9,
    "process_temperature_k": 309.1,
    "rotational_speed_rpm": 1420,
    "torque_nm": 46.3
  }
}
```

`predicted_failure_at` is calendar time derived from `remaining_cutting_min` with the detector's stated assumption (`CUTTING_MIN_PER_DAY`, default 20). `remaining_cutting_min` is the gap between the current tool wear and the wear at which the failure model starts flagging failure for these readings. Both use the model's score averaged over the last few readings. `risk` is the highest such score within the alert horizon (`ALERT_HORIZON_MIN`), so it is at least the alert threshold whenever an alert is sent: uncalibrated, not a probability. See `detector/model.py`.

## 2. AgentResult (agent service → app)

Response of `POST /incidents`:

```json
{
  "status": "awaiting_approval",
  "machine_id": "M-01",
  "diagnosis": "Cutting tool worn close to its limit.",
  "repair_steps": ["Stop spindle and lock out", "Replace end mill EM-1004", "..."],
  "part_number": "EM-1004",
  "part_name": "Carbide end mill 10 mm 4-flute",
  "part_in_stock": false,
  "purchase_order_id": "PO-0001",
  "expected_delivery": "2026-10-15T10:00:00+00:00",
  "booking_id": "BK-0001",
  "engineer_name": "Priya Nair",
  "booking_start": "2026-10-15T14:00:00+00:00",
  "booking_end": "2026-10-15T14:30:00+00:00",
  "escalation_reason": null,
  "operator_summary": "Plain-language text: what is wrong, what has been done, what happens next.",
  "warnings": [],
  "trace": [{"tool": "check_inventory", "arguments": {"part_number": "EM-1004"}, "result": {"...": "..."}}],
  "backend": "local"
}
```

- `status`: `awaiting_approval` (drafts created) or `escalated` (no engineer free in the window; the manager must act). `escalated` may still include a purchase order.
- `warnings`: guardrail findings, e.g. a part number not found in the manual or tools, or a booking outside the delivery → failure window. The app should show them to the manager.
- `trace`: every tool call, for the approval screen ("why the agent did this").
- Example part numbers and names above are placeholders: the real ones come from the manual and the mock seed data.

## 3. Mock factory APIs (base `MOCKS_URL`)

The agent (and the MCP server) only use the endpoints marked **agent**. Approve/reject is **app only** and must never be exposed as a tool.

### Inventory

| Method | Path | Used by | Response |
|---|---|---|---|
| GET | `/inventory/parts/{part_number}` | agent | `{"part_number", "name", "quantity_on_hand", "location"}`; 404 if unknown part |

### Supplier

| Method | Path | Used by | Body / response |
|---|---|---|---|
| GET | `/supplier/parts/{part_number}` | agent | `{"part_number", "supplier", "unit_price", "currency", "lead_time_hours"}`; 404 if not sold |
| POST | `/supplier/purchase-orders` | agent | body `{"part_number", "quantity", "machine_id", "reason"}` → `{"po_id", "status": "pending_approval", "part_number", "quantity", "expected_delivery", "total_price", "currency"}` |
| GET | `/supplier/purchase-orders/{po_id}` | app | the order |
| POST | `/supplier/purchase-orders/{po_id}/approve` | **app only** | order with `status: "approved"` |
| POST | `/supplier/purchase-orders/{po_id}/reject` | **app only** | order with `status: "rejected"` |

`expected_delivery` = creation time + `lead_time_hours`, rounded up to the whole hour; `total_price` = `unit_price` × `quantity`. `POST /supplier/purchase-orders` returns 404 for a part the supplier does not sell (same as the quote), so an invented part number can never be ordered.

### Engineer calendar

| Method | Path | Used by | Body / response |
|---|---|---|---|
| GET | `/calendar/engineers/free?skill=&earliest_start=&latest_end=&duration_minutes=` | agent | `[{"engineer_id", "name", "skills", "slot_start", "slot_end"}]` — free slots fully inside the window, may be empty |
| POST | `/calendar/bookings` | agent | body `{"engineer_id", "machine_id", "start", "end", "task", "purchase_order_id"}` → `{"booking_id", "status": "pending_approval", ...body}`; 409 if the slot is not free, or if `purchase_order_id` is given and `start` is before that order's `expected_delivery` |
| GET | `/calendar/bookings/{booking_id}` | app | the booking |
| POST | `/calendar/bookings/{booking_id}/approve` | **app only** | booking with `status: "approved"` |
| POST | `/calendar/bookings/{booking_id}/reject` | **app only** | booking with `status: "rejected"` |

Skill for tool-wear repairs: `cnc_tooling`. Accept times with `Z` or an offset; treat times without a time zone as UTC (the LLM sometimes drops it).

Implementation: `mocks/factory.py` (`MockFactory`, in-memory), served by `mocks/server.py`, seed data `mocks/seed.json`. Tests and the scenario eval use the same class with pinned test data (`devtools/fakes.py` → `FakeFactory`).

### Demo scenarios

| Method | Path | Used by | Effect |
|---|---|---|---|
| POST | `/admin/reset?scenario=out_of_stock\|in_stock\|no_engineer` | app / demo | reset seed data → `{"scenario", "started"}` |
| GET | `/health` | app / demo | `{"scenario", "note"}` |
| GET | `/admin/calls?after=<seq>` | agent service / app | `{"last", "calls": [{"seq", "tool", "arguments", "result"}]}`: every call to an **agent** endpoint, in the shape of the MCP tools' results (errors as `{"error": "<status>: <detail>"}`, free slots as `{"slots": [...]}`). The Evolus backend reads its run's trace from here. Cleared by a reset; `seq` keeps counting. |

Engineer slots are whole hours after the last reset (`hours_after_reset` in the seed), so reset just before each demo run (`detector.replay --scenario` does). The demo seed fits alerts with 25–45 cutting minutes left (`tests/test_mocks.py` checks this).

- `out_of_stock` (thin path): the tool-wear part has `quantity_on_hand: 0`; one engineer with a free slot after delivery and before failure.
- `in_stock`: the part is in stock; the agent should skip ordering.
- `no_engineer`: no free slot between delivery and failure; the agent should escalate.

## 4. MCP server

Streamable HTTP at `{MCP_URL}/mcp`, optional bearer token `MCP_API_TOKEN`. Tools (one per **agent** endpoint above):
`check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking`.
