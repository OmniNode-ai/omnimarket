# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output models for the LLM delegation routing compute node (OMN-11775)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_cost_basis import EnumCostBasis


class SkippedModel(BaseModel):
    """Audit entry for a model that was considered but not selected."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    model_id: str
    skip_reason: str


class ModelSelection(BaseModel):
    """The model selected by the routing algorithm."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    model_id: str
    tier: str
    endpoint_env: str
    cost_basis: EnumCostBasis
    selection_reason: str
    skipped_models: tuple[SkippedModel, ...]


class ModelDelegationRoutingOutput(BaseModel):
    """Output of the delegation routing compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    selection: ModelSelection
