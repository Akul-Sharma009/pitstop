"""Runs the configured agent backend and turns its plan into a checked AgentResult."""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import ValidationError

from agent import guard
from agent.models import AgentResult, Alert, ToolCall
from agent.reconcile import reconcile

log = logging.getLogger(__name__)


def make_agent(backend: str | None = None) -> Any:
    backend = backend or os.environ.get("AGENT_BACKEND", "local")
    if backend == "local":
        from agent.local_agent import LocalAgent

        return LocalAgent()
    if backend == "evolus":
        from agent.evolus_client import EvolusAgent

        return EvolusAgent()
    raise ValueError(f"Unknown AGENT_BACKEND {backend!r} (use local or evolus)")


def finalize(plan: dict[str, Any], trace: list[ToolCall], alert: Alert, backend: str) -> AgentResult:
    notes: list[str] = []
    if trace:  # local backend: facts come from what the tools returned
        plan, notes = reconcile(plan, trace)
    try:
        result = AgentResult(**{**plan, "machine_id": alert.machine_id, "trace": trace, "backend": backend})
    except ValidationError as exc:
        return failed(alert, backend, f"Agent returned an invalid plan: {exc.errors()[0]['msg']}", trace)
    result.warnings = notes + guard.check(result, alert)
    return result


def failed(alert: Alert, backend: str, reason: str, trace: list[ToolCall] | None = None) -> AgentResult:
    return AgentResult(
        status="failed",
        machine_id=alert.machine_id,
        escalation_reason=reason,
        operator_summary="The maintenance assistant could not finish a plan. A manager has been asked to look at it.",
        warnings=[reason],
        trace=trace or [],
        backend=backend,
    )


def run(alert: Alert, agent: Any = None, backend: str | None = None) -> AgentResult:
    backend = backend or os.environ.get("AGENT_BACKEND", "local")
    try:
        agent = agent or make_agent(backend)
        plan, trace = agent.run(alert)
    except Exception as exc:  # any backend failure becomes a visible, escalated result
        log.exception("Agent run failed")
        return failed(alert, backend, f"{type(exc).__name__}: {exc}")
    return finalize(plan, trace, alert, backend)
