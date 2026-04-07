# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelCloseoutInput — input to the closeout effect node.

Related:
    - OMN-7316: node_closeout_effect
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCloseoutInput(BaseModel):
    """Input to the closeout effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    dry_run: bool = Field(default=False, description="Skip actual side effects.")


__all__: list[str] = ["ModelCloseoutInput"]
