# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A nonempty evaluation batch."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)


class ModelDelegationGateEvalRequest(BaseModel):
    """A nonempty evaluation batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    items: tuple[ModelGateEvalItem, ...] = Field(min_length=1)
