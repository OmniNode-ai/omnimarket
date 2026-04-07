# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelVerifyInput — input to the verify effect node.

Related:
    - OMN-7581: migrate node_verify_effect to omnimarket
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelVerifyInput(BaseModel):
    """Input to the verify effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Build loop cycle correlation ID.")
    dry_run: bool = Field(default=False, description="Skip actual checks.")


__all__: list[str] = ["ModelVerifyInput"]
