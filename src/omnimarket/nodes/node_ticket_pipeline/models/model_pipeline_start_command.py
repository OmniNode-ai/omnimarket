"""ModelPipelineStartCommand — command to start the ticket pipeline."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_state import (
    EXECUTABLE_PHASES,
    EnumPipelinePhase,
)

_TICKET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


class ModelPipelineStartCommand(BaseModel):
    """Command to start the ticket pipeline for a given ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Pipeline run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    skip_test_iterate: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    skip_to: EnumPipelinePhase | None = Field(
        default=None, description="Resume from specified phase."
    )
    requested_at: datetime = Field(..., description="When the command was issued.")

    @field_validator("ticket_id")
    @classmethod
    def _validate_ticket_id(cls, value: str) -> str:
        if not _TICKET_ID_PATTERN.fullmatch(value):
            msg = "ticket_id must match an uppercase Linear key such as OMN-1234"
            raise ValueError(msg)
        return value

    @field_validator("skip_to")
    @classmethod
    def _validate_skip_to(
        cls, value: EnumPipelinePhase | None
    ) -> EnumPipelinePhase | None:
        if value is not None and value not in EXECUTABLE_PHASES:
            allowed = ", ".join(phase.value for phase in EXECUTABLE_PHASES)
            msg = f"skip_to must be one of: {allowed}"
            raise ValueError(msg)
        return value


__all__: list[str] = ["ModelPipelineStartCommand"]
