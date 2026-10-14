"""Runs the configured agent backend and turns its plan into a checked AgentResult."""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import ValidationError

from agent import guard
from agent.models import AgentResult, Alert, Downtime, Replan, ToolCall
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
    result.downtime = downtime(result)
    return result


def downtime(result: AgentResult) -> Downtime | None:
    """Planned swap vs unplanned breakdown, from the booked repair time and stated assumptions."""
    if result.status != "awaiting_approval" or not (result.booking_start and result.booking_end):
        return None
    planned_min = (result.booking_end - result.booking_start).total_seconds() / 60
    unplanned_h = float(os.environ.get("UNPLANNED_DOWNTIME_H", 8))
    cost_per_h = float(os.environ.get("DOWNTIME_COST_PER_H", 500))
    currency = os.environ.get("DOWNTIME_CURRENCY", "SGD")
    saved_h = max(unplanned_h - planned_min / 60, 0)
    return Downtime(
        planned_min=planned_min, unplanned_h=unplanned_h, saved_h=round(saved_h, 2), cost_per_h=cost_per_h,
        currency=currency, money_saved=round(saved_h * cost_per_h, 2),
        assumptions=f"An unplanned tool breakdown stops the machine for about {unplanned_h:g} h; "
                    f"downtime costs about {cost_per_h:g} {currency} per hour.",
    )


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


def run(alert: Alert, agent: Any = None, backend: str | None = None, replan: Replan | None = None) -> AgentResult:
    """Plan for an alert; with `replan`, plan again after the manager's feedback on the previous plan."""
    backend = backend or os.environ.get("AGENT_BACKEND", "local")
    try:
        agent = agent or make_agent(backend)
        plan, trace = agent.run(alert, replan) if replan else agent.run(alert)
    except Exception as exc:  # any backend failure becomes a visible, escalated result
        log.exception("Agent run failed")
        return failed(alert, backend, f"{type(exc).__name__}: {exc}")
    if replan and (order := replan.standing_order()):
        # The previous plan's order still stands: it is part of this plan, and the trace says where it came from.
        trace = [ToolCall(tool="draft_purchase_order", arguments={"from_previous_plan": True}, result=order), *trace]
    return finalize(plan, trace, alert, backend)
