# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_handler_correctness_gate.models.enums import (
    EnumScoringMethod,
)


class ModelEvalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input: str
    expected: str
    scoring: EnumScoringMethod = EnumScoringMethod.EXACT_MATCH
