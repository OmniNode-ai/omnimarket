# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's input: exactly one lab job transitioned event.

The shared frozen payload is imported rather than restated, so seam drift
fails validation. The writer strips the runtime's underscore-prefixed
injections before building this request.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lab_job.model_lab_job_transitioned import ModelLabJobTransitioned
from omnimarket.nodes.node_projection_lab_job.models.enum_lab_job_projection_event_kind import (
    EnumLabJobProjectionEventKind,
)


class ModelLabJobProjectionRequest(BaseModel):
    """One transitioned event, in the field named for its kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transitioned: ModelLabJobTransitioned = Field(...)

    @property
    def event_kind(self) -> EnumLabJobProjectionEventKind:
        return EnumLabJobProjectionEventKind.TRANSITIONED


__all__: list[str] = ["ModelLabJobProjectionRequest"]
