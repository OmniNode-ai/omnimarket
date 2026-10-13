# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Labels plus gate-eval items ready for the gate-eval compute (OMN-19787)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.delegation_gate_eval.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.models.delegation_gate_eval.model_ground_truth_label import (
    ModelGroundTruthLabel,
)


class ModelGroundTruthLabelResult(BaseModel):
    """``items`` carry the gate's recorded verdict beside the independent label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str = Field(min_length=1)
    labels: tuple[ModelGroundTruthLabel, ...]
    items: tuple[ModelGateEvalItem, ...]
