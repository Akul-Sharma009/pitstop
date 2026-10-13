"""Agent service: POST an Alert, get back an AgentResult (docs/contracts.md).

Run: uvicorn agent.api:app --port 8002
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from agent import manual, runner
from agent.models import AgentResult, Alert

app = FastAPI(title="Pitstop agent")


@app.get("/health")
def health() -> dict[str, str | int]:
    sections = len(manual.load_sections())
    return {"status": "ok" if sections else "no manual", "backend": os.environ.get("AGENT_BACKEND", "local"),
            "manual_sections": sections}


@app.post("/incidents", response_model=AgentResult)
async def create_incident(alert: Alert) -> AgentResult:
    # The LLM loop is blocking and can take a minute; keep the event loop free.
    return await run_in_threadpool(runner.run, alert)
