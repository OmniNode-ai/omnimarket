# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_set import (
    ModelEvalSet,
)


class ModelCorrectnessCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    handler_id: str
    eval_set: ModelEvalSet
    actual_outputs: tuple[str, ...]
    correlation_id: str
