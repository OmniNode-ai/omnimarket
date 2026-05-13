# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""A single model entry within a delegation routing tier."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTierModel(BaseModel):
    """A single model entry within a tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(..., description="Model identifier.")
    backend_ref: str = Field(
        ...,
        description="Backend key in the deployed bifrost contract (bifrost_delegation.yaml).",
    )
    max_context_tokens: int = Field(..., description="Max context window in tokens.")
    use_for: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Task types this model handles.",
    )
    fast_path_threshold_tokens: int | None = Field(
        default=None,
        description="If set, prefer this model when prompt tokens <= threshold.",
    )


__all__: list[str] = ["ModelTierModel"]
