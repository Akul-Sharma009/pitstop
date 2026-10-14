"""Agent service: POST an Alert, get back an AgentResult (docs/contracts.md).

GET /incidents/latest returns the latest alert and its plan (null while the agent is still planning), so the
screens update by themselves when the detector fires.

Run: uvicorn agent.api:app --port 8002
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from agent import manual, runner
from agent.models import AgentResult, Alert

app = FastAPI(title="Pitstop agent")
latest: dict[str, Any] = {}  # the demo handles one machine at a time


@app.get("/health")
def health() -> dict[str, str | int]:
    sections = len(manual.load_sections())
    return {"status": "ok" if sections else "no manual", "backend": os.environ.get("AGENT_BACKEND", "local"),
            "manual_sections": sections}


@app.post("/incidents", response_model=AgentResult)
async def create_incident(alert: Alert) -> AgentResult:
    received = datetime.now(timezone.utc).isoformat()
    latest.update(alert=alert.model_dump(mode="json"), result=None, received_at=received)
    # The LLM loop is blocking and can take a minute; keep the event loop free.
    result = await run_in_threadpool(runner.run, alert)
    if latest.get("received_at") == received:  # a newer alert may have arrived meanwhile
        latest["result"] = result.model_dump(mode="json")
    return result


@app.get("/incidents/latest")
def latest_incident() -> dict[str, Any]:
    """{"alert", "result" (null while planning), "received_at"}; 404 before the first alert."""
    if not latest:
        raise HTTPException(404, "no alert yet")
    return latest
