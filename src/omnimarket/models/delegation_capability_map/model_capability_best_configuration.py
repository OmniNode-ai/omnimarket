# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The best and next-best measured configuration for a model on a class at a band."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_size_band import EnumSizeBand


class ModelCapabilityBestConfiguration(BaseModel):
    """A deterministic read of MET cells ordered by decision lower bound (plan 13.10).

    Absent when no cell for the model, backend, class and band is MET.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    map_run_id: str = Field(min_length=1)
    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    size_band: EnumSizeBand
    best_configuration_id: str = Field(min_length=1)
    best_lower_bound: float = Field(ge=0.0, le=1.0)
    next_best_configuration_id: str | None = Field(default=None, min_length=1)
    next_best_lower_bound: float | None = Field(default=None, ge=0.0, le=1.0)
