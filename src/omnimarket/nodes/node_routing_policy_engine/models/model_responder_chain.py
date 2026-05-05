# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Responder-chain configuration models for routing policy tiers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CapabilityTier = Literal["C1", "C2", "C3", "C4"]

_REQUIRED_TIERS: frozenset[str] = frozenset(("C1", "C2", "C3", "C4"))


class ModelResponderModel(BaseModel):
    """A single model option in a responder chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(..., min_length=1, description="Model provider key.")
    model: str = Field(..., min_length=1, description="Provider model identifier.")


class ModelResponderChain(BaseModel):
    """Ordered model options for one capability tier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: CapabilityTier = Field(..., description="Capability tier served by chain.")
    description: str = Field(default="", description="Human-readable chain purpose.")
    models: tuple[ModelResponderModel, ...] = Field(
        ..., min_length=1, description="Ordered responder models for this tier."
    )


class ModelResponderChainConfig(BaseModel):
    """Complete responder-chain configuration for routing policy engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chains: tuple[ModelResponderChain, ...] = Field(
        ..., min_length=4, description="Responder chains keyed by capability tier."
    )

    @model_validator(mode="after")
    def validate_required_tiers(self) -> ModelResponderChainConfig:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for chain in self.chains:
            if chain.tier in seen:
                duplicates.add(chain.tier)
            seen.add(chain.tier)

        if duplicates:
            joined = ", ".join(sorted(duplicates))
            raise ValueError(f"Duplicate responder chain tiers: {joined}")

        missing = _REQUIRED_TIERS - seen
        if missing:
            joined = ", ".join(sorted(missing))
            raise ValueError(f"Missing responder chain tiers: {joined}")

        return self

    @classmethod
    def from_yaml(cls, path: Path) -> ModelResponderChainConfig:
        """Load and validate responder-chain configuration from YAML."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    def chain_for_tier(self, tier: CapabilityTier) -> ModelResponderChain:
        """Return the responder chain for a capability tier."""
        for chain in self.chains:
            if chain.tier == tier:
                return chain
        raise KeyError(tier)


__all__: list[str] = [
    "CapabilityTier",
    "ModelResponderChain",
    "ModelResponderChainConfig",
    "ModelResponderModel",
]
