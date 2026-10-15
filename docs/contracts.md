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

`simulated` (optional, default `false`; fleet mode, in progress): `true` for an alert the detector did not produce. In fleet mode M-04's coolant-pump alert is simulated, since the failure model only flags tool wear; the screens label it.

`predicted_failure_at` is calendar time derived from `remaining_cutting_min` with the detector's stated assumption (`CUTTING_MIN_PER_DAY`, default 20). `remaining_cutting_min` is the gap between the current tool wear and the wear at which the failure model starts flagging failure for these readings. Both use the model's score averaged over the last few readings. `risk` is the highest such score within the alert horizon (`ALERT_HORIZON_MIN`), so it is at least the alert threshold whenever an alert is sent: uncalibrated, not a probability. See `detector/model.py`.

## 2. AgentResult (agent service → app)

`GET {AGENT_URL}/incidents/latest` returns `{"alert", "result", "received_at", "feedback", "previous"}` for the most recent alert of any machine; `result` is `null` while the agent is still planning; `feedback` and `previous` (the plan the manager rejected) are set after a replan; 404 before the first alert. The screens follow it (section 5).

**Fleet mode** (several machines plan at once, sharing one factory). Incidents are kept per machine:

| Method | Path | Returns |
|---|---|---|
| GET | `/incidents` | `{"incidents": [ … ]}`: every machine's latest alert and plan, sorted by machine ID; each entry has the shape of `/incidents/latest` |
| GET | `/incidents/{machine_id}` | that machine's latest alert and plan; 404 if it has none |
| DELETE | `/incidents` | forgets every incident → `{"cleared": n}` (the replay calls it when it resets the factory) |

The local backend plans machines in parallel. The Evolus backend queues its runs one at a time and sets the factory's focus (section 3) before each, since MCP tool calls don't say which machine they are for.

**Replan.** `POST {AGENT_URL}/incidents/replan` with `{"feedback": "Priya is on leave on Thursday", "machine_id": "M-01"|null, "purchase_order": <order>|null, "booking": <booking>|null}`: the previous plan's drafts as the mocks have them now (the app sends them after a rejection, with the machine of the decided order or booking). The agent plans that machine's alert again with the feedback (without `machine_id`: the most recent alert's machine). A purchase order that is not rejected still stands: the new plan reuses it (it appears first in `trace` as `draft_purchase_order` with `{"from_previous_plan": true}`) and must not order again. Returns the new AgentResult; 404 without an alert (for that machine), 409 while the agent is still planning.

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
  "downtime": {
    "planned_min": 30,
    "unplanned_h": 8,
    "saved_h": 7.5,
    "cost_per_h": 500,
    "currency": "SGD",
    "money_saved": 3750,
    "assumptions": "An unplanned tool breakdown stops the machine for about 8 h; downtime costs about 500 SGD per hour."
  },
  "warnings": [],
  "trace": [{"tool": "check_inventory", "arguments": {"part_number": "EM-1004"}, "result": {"...": "..."}}],
  "backend": "local"
}
```

- `status`: `awaiting_approval` (drafts created) or `escalated` (no engineer free in the window; the manager must act). `escalated` may still include a purchase order.
- `downtime`: downtime saved by the planned swap, for the operator summary screen. Computed by the agent service, not the model: `planned_min` is the booked repair time; `unplanned_h` and `cost_per_h` are stated assumptions (env `UNPLANNED_DOWNTIME_H`, `DOWNTIME_COST_PER_H`, `DOWNTIME_CURRENCY`), shown with `assumptions`. `null` unless a repair is booked.
- `warnings`: guardrail findings, e.g. a part number not found in the manual or tools, or a booking outside the delivery → failure window. The app should show them to the manager.
- `trace`: every tool call, for the approval screen ("why the agent did this").
- Example part numbers and names above are placeholders: the real ones come from the manual and the mock seed data.

## 3. Mock factory APIs (base `MOCKS_URL`)

The agent (and the MCP server) only use the endpoints marked **agent**. Approve/reject is **app only** and must never be exposed as a tool.

**Machine context (fleet mode).** Several machines share the engineers and the parts store. A call is for the machine in its `X-Machine-Id` header (the local agent sends it), or else for the factory's focus (`POST /admin/focus`, set by the Evolus backend before each run). Without either, the factory behaves as for a single machine. Every logged call records its `machine_id`.

### Inventory

| Method | Path | Used by | Response |
|---|---|---|---|
| GET | `/inventory/parts/{part_number}` | agent | `{"part_number", "name", "quantity_on_hand", "location"}`; 404 if unknown part. `quantity_on_hand` is the stock minus the units reserved by bookings the manager has not rejected |

### Supplier

| Method | Path | Used by | Body / response |
|---|---|---|---|
| GET | `/supplier/parts/{part_number}` | agent | `{"part_number", "offers": [{"supplier", "unit_price", "currency", "lead_time_hours", "expected_delivery"}]}`, cheapest first; `expected_delivery` if ordered now; 404 if nobody sells it |
| POST | `/supplier/purchase-orders` | agent | body `{"part_number", "quantity", "machine_id", "reason", "supplier"?}` (default: the cheapest offer) → `{"po_id", "status": "pending_approval", "part_number", "quantity", "supplier", "expected_delivery", "total_price", "currency"}`; 404 if that supplier doesn't sell the part |
| GET | `/supplier/purchase-orders/{po_id}` | app | the order |
| POST | `/supplier/purchase-orders/{po_id}/approve` | **app only** | order with `status: "approved"` |
| POST | `/supplier/purchase-orders/{po_id}/reject` | **app only** | order with `status: "rejected"` |

The agent orders from the cheapest offer that arrives in time to repair before the predicted failure (the guard warns when a dearer one was chosen without need, or when the order arrives after the predicted failure while another offer was in time; the local agent refuses such an order). One open order per part and machine: `POST /supplier/purchase-orders` returns 409 (naming the open order) while a non-rejected order for the same part and machine exists. `expected_delivery` = creation time + `lead_time_hours`, rounded up to the whole hour; `total_price` = `unit_price` × `quantity`. `POST /supplier/purchase-orders` returns 404 for a part the supplier does not sell (same as the quote), so an invented part number can never be ordered.

### Engineer calendar

| Method | Path | Used by | Body / response |
|---|---|---|---|
| GET | `/calendar/engineers/free?skill=&earliest_start=&latest_end=&duration_minutes=` | agent | `[{"engineer_id", "name", "skills", "slot_start", "slot_end"}]` — free slots fully inside the window, may be empty (a slot held by a booking is not free); 422 if the window is empty (`earliest_start` not before `latest_end`, e.g. the part arrives after the predicted failure); 409 if a part is on order for this machine and `earliest_start` is before its `expected_delivery` (demo rule: the repair can't start before the part arrives; another machine's order doesn't count) |
| POST | `/calendar/bookings` | agent | body `{"engineer_id", "machine_id", "start", "end", "task", "purchase_order_id", "part_number"?}` → `{"booking_id", "status": "pending_approval", "reserved_part"?, ...body}`; 409 if the slot is not free or already held by another booking (naming it), if `purchase_order_id` is given and `start` is before that order's `expected_delivery`, or if the part comes from stock and none is left (naming the bookings that reserved it) |
| GET | `/calendar/bookings/{booking_id}` | app | the booking |
| POST | `/calendar/bookings/{booking_id}/approve` | **app only** | booking with `status: "approved"` |
| POST | `/calendar/bookings/{booking_id}/reject` | **app only** | booking with `status: "rejected"` |

A booking holds its slot, and a booking without a purchase order reserves one unit of its part from stock (`part_number`, or else the part this machine last found in stock with `check_inventory`), until the manager rejects it. Approval keeps both. Skill for tool-wear repairs: `cnc_tooling`. Accept times with `Z` or an offset; treat times without a time zone as UTC (the LLM sometimes drops it).

Implementation: `mocks/factory.py` (`MockFactory`, in-memory), served by `mocks/server.py`, seed data `mocks/seed.json`. Tests and the scenario eval use the same class with pinned test data (`devtools/fakes.py` → `FakeFactory`).

### Demo scenarios

| Method | Path | Used by | Effect |
|---|---|---|---|
| POST | `/admin/reset?scenario=out_of_stock\|in_stock\|no_engineer\|urgent\|fleet` | app / demo | reset seed data → `{"scenario", "started"}`; clears the focus |
| POST | `/admin/focus?machine_id=` | agent service (Evolus backend) | the machine that calls without `X-Machine-Id` are for → `{"focus"}`; empty clears it |
| GET | `/health` | app / demo | `{"scenario", "note"}` |
| GET | `/admin/calls?after=<seq>` | agent service / app | `{"last", "calls": [{"seq", "tool", "arguments", "result", "machine_id"}]}`: every call to an **agent** endpoint, in the shape of the MCP tools' results (errors as `{"error": "<status>: <detail>"}`, free slots as `{"slots": [...]}`). The Evolus backend reads its run's trace from here. Cleared by a reset; `seq` keeps counting. |

Engineer slots are whole hours after the last reset (`hours_after_reset` in the seed), so reset just before each demo run (`detector.replay --scenario` does). The demo seed fits alerts with 25–45 cutting minutes left (`tests/test_mocks.py` checks this).

- `out_of_stock` (thin path): the tool-wear part has `quantity_on_hand: 0`; one engineer with a free slot after delivery and before failure.
- `in_stock`: the part is in stock; the agent should skip ordering.
- `no_engineer`: no free slot between delivery and failure; the agent should escalate.
- `urgent`: only the fast supplier arrives in time (run the detector with `CUTTING_MIN_PER_DAY=60`).
- `fleet`: four machines share one EM-1004 in stock and the engineers (Priya at +7 h and +26 h, Wei Ling at +27 h, Daniel at +26 h): one tool-wear machine uses the stock part, two order it and are booked after delivery, and M-04's coolant pump (none in stock, 72 h lead time) can't arrive before its failure, so it escalates.

## 4. MCP server

Streamable HTTP at `{MCP_URL}/mcp`, optional bearer token `MCP_API_TOKEN`. Tools (one per **agent** endpoint above):
`check_inventory`, `get_supplier_quote`, `draft_purchase_order`, `find_free_engineers`, `draft_booking` (with optional `part_number`, for a part from stock).

## 5. Screens (`app/`, port 8003)

The manager approval screen and the operator summary (`app/static`), served with a small API so the page needs one address. This is the app side of the approval boundary.

| Method | Path | Does |
|---|---|---|
| GET | `/api/latest` | `GET {AGENT_URL}/incidents/latest`; `{"alert": null, "result": null}` before the first alert |
| GET | `/api/incidents` | fleet mode: `{"incidents": [ … ]}`, one entry per machine sorted by machine ID; each entry has the shape of `/api/latest` |
| GET | `/api/incidents/{machine_id}` | fleet mode: that machine's latest alert and plan (shape of `/api/latest`); 404 if the machine has no alert |
| GET | `/api/purchase-orders/{id}`, `/api/bookings/{id}` | the order or booking from the mocks (its approval status) |
| POST | `/api/purchase-orders/{id}/approve\|reject`, `/api/bookings/{id}/approve\|reject` | the manager's decision, to the mocks' **app only** endpoints. A reject with body `{"reason": "..."}` also starts a replan in the background (`POST {AGENT_URL}/incidents/replan`) for the machine of the decided order or booking |
