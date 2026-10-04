# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Three supplied generation verdicts for one item on one backend."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.enum_gate_verdict import EnumGateVerdict


class ModelGenerationStabilityGroup(BaseModel):
    """One prompt generated three times, distinct from deterministic gate replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    verdicts: tuple[EnumGateVerdict, EnumGateVerdict, EnumGateVerdict]
