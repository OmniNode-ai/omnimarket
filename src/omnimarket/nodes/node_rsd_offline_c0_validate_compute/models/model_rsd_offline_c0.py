# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for pure, non-authorizing C0 route validation."""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.runtime.golden_chain.model_golden_chain_fixture import (
    ModelGoldenChainProvenance,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


class ModelRsdOfflineC0Input(BaseModel):
    """Explicit contract evidence for a single already-made routing decision.

    The input never selects a route. ``routing_decision`` is the canonical
    reducer output and every byte payload is supplied by the caller solely for
    offline consistency validation against recorded golden-chain provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    routing_decision: ModelRoutingDecision
    golden_provenance: ModelGoldenChainProvenance
    routing_tiers_yaml: bytes = Field(min_length=1, max_length=524_288)
    bifrost_contract_yaml: bytes = Field(min_length=1, max_length=1_048_576)
    bifrost_overlay_yaml: bytes | None = Field(default=None, max_length=1_048_576)
    model_registry_yaml: bytes = Field(min_length=1, max_length=524_288)
    model_registry_key: str = Field(min_length=1, max_length=256)
    served_model_environment: str | None = Field(default=None, max_length=128)


class ModelRsdOfflineC0Output(BaseModel):
    """Offline diagnostic evidence; it cannot authorize or dispatch anything."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    routing_contract_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    routing_overlay_hash: str = Field(pattern=r"^(?:none|sha256:[0-9a-f]{64})$")
    model_registry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tier_name: str = Field(min_length=1)
    backend_ref: str = Field(min_length=1)
    routing_tier_model_id: str = Field(min_length=1)
    model_registry_key: str = Field(min_length=1)
    served_model: str = Field(min_length=1)
    served_model_environment: str | None = None
    non_authorizing: Literal[True] = True
    effects_allowed: Literal[False] = False


__all__ = ["ModelRsdOfflineC0Input", "ModelRsdOfflineC0Output"]
