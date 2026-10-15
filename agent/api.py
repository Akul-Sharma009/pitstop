"""Agent service: POST an Alert, get back an AgentResult (docs/contracts.md).

Incidents are kept per machine (fleet mode: several machines plan at once). GET /incidents lists the latest
alert and plan of every machine, GET /incidents/{machine_id} one machine, GET /incidents/latest the most
recent alert of any machine (the single-machine screens); the plan is null while the agent is still
planning, so the screens update by themselves when the detector fires. POST /incidents/replan plans a
machine's alert again after the manager rejected part of its plan, with their reason. DELETE /incidents
forgets every incident (the replay calls it when it resets the factory).

Run: uvicorn agent.api:app --port 8002
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from agent import manual, runner
from agent.models import AgentResult, Alert, Replan

app = FastAPI(title="Pitstop agent")
incidents: dict[str, dict[str, Any]] = {}  # machine_id → {"alert", "result", "received_at", "feedback", "previous"}


@app.get("/health")
def health() -> dict[str, str | int]:
    sections = len(manual.load_sections())
    return {"status": "ok" if sections else "no manual", "backend": os.environ.get("AGENT_BACKEND", "local"),
            "manual_sections": sections}


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
    return await _plan(alert)


@app.post("/incidents/replan", response_model=AgentResult)
async def replan_incident(replan: Replan) -> AgentResult:
    """Plan a machine's alert again with the manager's feedback (sent by the app after a rejection).
    Without machine_id: the most recent alert's machine."""
    incident = incidents.get(replan.machine_id) if replan.machine_id else _latest()
    if not incident:
        raise HTTPException(404, "no alert to replan")
    if incident.get("result") is None:
        raise HTTPException(409, "the agent is still planning")
    return await _plan(Alert(**incident["alert"]), replan)


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
