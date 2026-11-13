# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nudge: the tick route's per-run message (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/models/``. Time comes from the
message, so the reducer never reads a clock.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEvalRunNudge(BaseModel):
    """``onex.cmd.omnimarket.code-eval-run-nudge.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    now: datetime
    tick_id: str = Field(min_length=1)


__all__ = ["ModelCodeEvalRunNudge"]
