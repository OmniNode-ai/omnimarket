# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for deterministic context experiment assembly."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_context_experiment_compute.models.model_context_chunk_extended import (
    ModelContextChunkExtended,
)


class ModelContextExperimentRequest(BaseModel):
    """Inputs required to assemble one context pack per factor subset."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    factor_subsets: tuple[tuple[EnumContextFactor, ...], ...] = Field(min_length=1)
    artifacts: tuple[ModelContextChunkExtended, ...]
    contract_hash: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    profile_version: str = Field(default="1.0.0", min_length=1)
    generator_version: str = Field(default="1.0.0", min_length=1)
    harness_kind: str = Field(default="context_experiment", min_length=1)
    execution_mode: str = Field(default="batch", min_length=1)
    task_class: str = Field(default="context_experiment", min_length=1)
    topology_class: str = Field(default="context_factor_subset", min_length=1)


__all__ = ["ModelContextExperimentRequest"]
