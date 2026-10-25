# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The event one map run publishes: its verdict rows and serving assignments (OMN-20169)."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.delegation_capability_map.model_capability_map_fold import (
    ModelCapabilityMapFoldRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)


class ModelCapabilityMapRunRecorded(BaseModel):
    """One tenant's map run, content free: verdicts and identities, never a prompt or answer.

    ``observed_at`` is the producer's time; a redelivered run with the same time
    writes no row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    map_run_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    rows: tuple[ModelCapabilityVerdictRow, ...] = ()
    assignments: tuple[ModelCapabilityServingAssignment, ...] = ()
    line: ModelCapabilityLine = Field(default_factory=ModelCapabilityLine)

    @model_validator(mode="after")
    def _one_tenant(self) -> Self:
        others = {row.tenant_id for row in self.rows} - {self.tenant_id}
        if others:
            raise ValueError("every verdict row of a map run carries the run's tenant")
        return self

    def fold_request(self) -> ModelCapabilityMapFoldRequest:
        return ModelCapabilityMapFoldRequest(
            map_run_id=self.map_run_id,
            rows=self.rows,
            assignments=self.assignments,
            line=self.line,
        )
