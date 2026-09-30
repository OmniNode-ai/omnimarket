# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run-operation result: the one terminal payload the runtime publishes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events import delegation_eval


class ModelEvalRunResult(BaseModel):
    """Payload for the runtime-owned publishing effect (OMN-19793)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: delegation_eval.ModelDelegationEvalRunCompleted
