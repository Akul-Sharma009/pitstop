"""Alert and AgentResult, as defined in docs/contracts.md."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Alert(BaseModel):
    machine_id: str
    failure_type: str
    risk: float
    tool_wear_min: float | None = None
    remaining_cutting_min: float | None = None
    detected_at: datetime
    predicted_failure_at: datetime
    readings: dict[str, float] = Field(default_factory=dict)


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any]
    result: Any


class AgentResult(BaseModel):
    status: Literal["awaiting_approval", "escalated", "failed"]
    machine_id: str
    diagnosis: str = ""
    repair_steps: list[str] = Field(default_factory=list)
    part_number: str | None = None
    part_name: str | None = None
    part_in_stock: bool | None = None
    purchase_order_id: str | None = None
    expected_delivery: datetime | None = None
    booking_id: str | None = None
    engineer_name: str | None = None
    booking_start: datetime | None = None
    booking_end: datetime | None = None
    escalation_reason: str | None = None
    operator_summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    trace: list[ToolCall] = Field(default_factory=list)
    backend: str = ""
