"""Agent service: POST an Alert, get back an AgentResult (docs/contracts.md).

Incidents are kept per machine (fleet mode: several machines plan at once). GET /incidents lists the latest
alert and plan of every machine, GET /incidents/{machine_id} one machine, GET /incidents/latest the most
recent alert of any machine (the single-machine screens); the plan is null while the agent is still
planning, so the screens update by themselves when the detector fires. POST /incidents/replan plans a
machine's alert again after the manager rejected part of its plan, with their reason (and, if the sensor got
worse, the newer alert). DELETE /incidents forgets every incident (the replay calls it when it resets the factory).

A new alert for a machine whose plan is still open (awaiting approval, same failure, no draft rejected) does
not run the agent again: the plan is kept, so the manager isn't asked twice and no second booking is drafted.
If the failure is now predicted before the booked repair ends, the plan is kept with a warning: only the
manager cancels a booking (the app's "sensor got worse" demo control does it for a pending one and replans).

Run: uvicorn agent.api:app --port 8002
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from agent import manual, runner
from agent.models import AgentResult, Alert, Replan
from agent.times import readable, utc

app = FastAPI(title="Pitstop agent")
# machine_id → {"alert", "result", "received_at", "feedback", "previous"} (+ "repeats": alerts that kept the plan)
incidents: dict[str, dict[str, Any]] = {}


MODEL_CHECK_S = 30  # how long a model check is reused (the screens poll often)
_model_check: dict[str, Any] = {}


def model_status() -> str:
    """'online' if our model endpoint (VLLM_BASE_URL, the vLLM server or its stand-in) lists models, else
    'offline' (e.g. the GPU instance is switched off to save credits). Both backends use this model."""
    now = time.monotonic()
    if _model_check and now - _model_check["at"] < MODEL_CHECK_S:
        return _model_check["status"]
    base = os.environ.get("VLLM_BASE_URL")
    status = "unknown"
    if base:
        try:
            response = httpx.get(f"{base.rstrip('/')}/models", timeout=3,
                                 headers={"Authorization": f"Bearer {os.environ.get('VLLM_API_KEY', 'EMPTY')}"})
            status = "online" if response.status_code == 200 else "offline"
        except httpx.HTTPError:
            status = "offline"
    _model_check.update(at=now, status=status)
    return status


@app.get("/health")
def health() -> dict[str, str | int]:
    sections = len(manual.load_sections())
    return {"status": "ok" if sections else "no manual", "backend": os.environ.get("AGENT_BACKEND", "local"),
            "manual_sections": sections, "model": model_status()}


async def _plan(alert: Alert, replan: Replan | None = None) -> AgentResult:
    received = datetime.now(timezone.utc).isoformat()
    previous = incidents.get(alert.machine_id, {}).get("result") if replan else None
    incidents[alert.machine_id] = {"alert": alert.model_dump(mode="json"), "result": None, "received_at": received,
                                   "feedback": replan.feedback if replan else None, "previous": previous}
    # The LLM loop is blocking and can take a minute; keep the event loop free (other machines plan meanwhile).
    result = await run_in_threadpool(runner.run, alert, None, None, replan)
    incident = incidents.get(alert.machine_id)
    if incident and incident["received_at"] == received:  # a newer alert for this machine may have arrived
        incident["result"] = result.model_dump(mode="json")
    return result


@app.post("/incidents", response_model=AgentResult)
async def create_incident(alert: Alert) -> AgentResult:
    incident = incidents.get(alert.machine_id)
    if incident and incident["result"] is None:
        raise HTTPException(409, f"the agent is still planning for {alert.machine_id}")
    if incident and (kept := await run_in_threadpool(_repeat, incident, alert)):
        return kept
    return await _plan(alert)


def draft_status(kind: str, item_id: str | None) -> str | None:
    """A draft's status in the mock factory (read only); None if there is no draft or the factory can't say."""
    if not item_id:
        return None
    path = {"order": "/supplier/purchase-orders", "booking": "/calendar/bookings"}[kind]
    try:
        response = httpx.get(f"{os.environ.get('MOCKS_URL', 'http://localhost:8000')}{path}/{item_id}", timeout=5)
        return response.json().get("status") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _repeat(incident: dict[str, Any], alert: Alert) -> AgentResult | None:
    """The machine's open plan, kept for a new alert; None if there is no open plan to keep."""
    result = incident["result"]
    if result["status"] != "awaiting_approval" or incident["alert"]["failure_type"] != alert.failure_type:
        return None
    drafts = [draft_status("order", result.get("purchase_order_id")), draft_status("booking", result.get("booking_id"))]
    if "rejected" in drafts:  # the plan is no longer whole (a rejection without a reason): plan afresh
        return None
    end = utc(result.get("booking_end"))
    if end and end > alert.predicted_failure_at:
        result["warnings"] = [*result["warnings"], (
            f"New alert {readable(alert.detected_at)}: the failure is now predicted {readable(alert.predicted_failure_at)}, "
            f"before the booked repair ends ({readable(end)}). Reject booking {result.get('booking_id')} with a reason "
            "to get a new plan.")]
    incident.update(alert=alert.model_dump(mode="json"), repeats=incident.get("repeats", 0) + 1)
    return AgentResult(**result)


@app.post("/incidents/replan", response_model=AgentResult)
async def replan_incident(replan: Replan) -> AgentResult:
    """Plan a machine's alert again with the manager's feedback (sent by the app after a rejection).
    Without machine_id: the most recent alert's machine."""
    incident = incidents.get(replan.machine_id) if replan.machine_id else _latest()
    if not incident:
        raise HTTPException(404, "no alert to replan")
    if incident.get("result") is None:
        raise HTTPException(409, "the agent is still planning")
    return await _plan(replan.alert or Alert(**incident["alert"]), replan)


def _latest() -> dict[str, Any] | None:
    return max(incidents.values(), key=lambda i: i["received_at"], default=None)


@app.get("/incidents")
def list_incidents() -> dict[str, list[dict[str, Any]]]:
    """Every machine's latest alert and plan, sorted by machine ID."""
    return {"incidents": [incidents[m] for m in sorted(incidents)]}


@app.delete("/incidents")
def clear_incidents() -> dict[str, int]:
    count = len(incidents)
    incidents.clear()
    return {"cleared": count}


@app.get("/incidents/latest")
def latest_incident() -> dict[str, Any]:
    """{"alert", "result" (null while planning), "received_at", "feedback" and "previous" (the plan the
    manager rejected, when replanning)} for the most recent alert of any machine; 404 before the first alert."""
    incident = _latest()
    if not incident:
        raise HTTPException(404, "no alert yet")
    return incident


@app.get("/incidents/{machine_id}")
def machine_incident(machine_id: str) -> dict[str, Any]:
    """One machine's latest alert and plan; 404 if the machine has no alert."""
    if machine_id not in incidents:
        raise HTTPException(404, f"no alert for {machine_id}")
    return incidents[machine_id]
