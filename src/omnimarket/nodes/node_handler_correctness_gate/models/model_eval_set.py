# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_entry import (
    ModelEvalEntry,
)


class ModelEvalSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelEvalEntry, ...]
    min_score: float = 0.85
    name: str = ""
    description: str = ""
