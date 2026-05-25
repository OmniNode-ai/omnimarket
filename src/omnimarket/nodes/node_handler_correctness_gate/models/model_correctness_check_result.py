# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_failure import (
    ModelEvalFailure,
)


class ModelCorrectnessCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    handler_id: str
    score: float
    passed: bool
    total_entries: int
    correct_entries: int
    failures: tuple[ModelEvalFailure, ...] = ()
    eval_set_name: str = ""
