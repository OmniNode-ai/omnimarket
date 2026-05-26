# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_handler_correctness_gate.models.enums import (
    EnumScoringMethod,
)


class ModelEvalFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_index: int
    input: str
    expected: str
    actual: str
    scoring: EnumScoringMethod
