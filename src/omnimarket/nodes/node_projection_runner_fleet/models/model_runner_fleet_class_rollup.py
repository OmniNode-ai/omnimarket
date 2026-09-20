# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-label-class rollup of a fleet observation (OMN-18768)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRunnerFleetClassRollup(BaseModel):
    """Counts for one label class.

    Which CLASS is down is the operational question. A fleet-wide healthy count
    hides a total outage of a single-runner class -- and the classes are not
    interchangeable: ``omnibase-prod-deploy`` serves production promotion,
    ``omnibase-ci`` is dozens of substitutable runners.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label_class: str = Field(min_length=1)
    total: int = Field(ge=0)
    online: int = Field(ge=0)
    busy: int = Field(ge=0)
    offline: int = Field(ge=0)
