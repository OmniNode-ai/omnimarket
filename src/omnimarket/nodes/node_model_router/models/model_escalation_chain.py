# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelEscalationChain — tiered model escalation for node_model_router.

Tier order: local -> cheap_cloud -> mid_frontier -> expensive_frontier.
expensive_frontier is NEVER included in auto-escalation.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict, Field

RegistryEntry = dict[str, str]
Registry = dict[str, RegistryEntry]


class EscalationTier(IntEnum):
    """Ordered tier values — lower is cheaper/more local."""

    local = 0
    cheap_cloud = 1
    mid_frontier = 2
    expensive_frontier = 3

    @classmethod
    def from_str(cls, value: str) -> EscalationTier:
        try:
            return cls[value]
        except KeyError:
            return cls.local


class ModelEscalationLevel(BaseModel):
    """A single tier in the escalation chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: EscalationTier
    model_keys: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1)


class ModelEscalationChain(BaseModel):
    """Full escalation chain grouped by tier.

    Use from_registry() to construct from a flat registry dict.
    auto_escalation_tiers() returns tiers eligible for automatic escalation
    (always excludes expensive_frontier).
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    levels: dict[EscalationTier, ModelEscalationLevel] = Field(default_factory=dict)

    @classmethod
    def from_registry(
        cls, registry: Registry, max_attempts_per_tier: int = 2
    ) -> ModelEscalationChain:
        """Build escalation chain from registry entries.

        Registry entries with a 'tier' key are grouped. Entries without a tier
        key default to 'local'.
        """
        grouped: dict[EscalationTier, list[str]] = {}
        for tier in EscalationTier:
            grouped[tier] = []

        for model_key, entry in registry.items():
            tier = EscalationTier.from_str(entry.get("tier", "local"))
            grouped[tier].append(model_key)

        levels: dict[EscalationTier, ModelEscalationLevel] = {}
        for tier, keys in grouped.items():
            if keys:
                levels[tier] = ModelEscalationLevel(
                    tier=tier,
                    model_keys=sorted(keys),
                    max_attempts=max_attempts_per_tier,
                )

        return cls(levels=levels)

    def auto_escalation_tiers(self) -> list[EscalationTier]:
        """Return tiers eligible for automatic escalation (excludes expensive_frontier)."""
        return [
            t
            for t in sorted(self.levels.keys())
            if t != EscalationTier.expensive_frontier
        ]

    def next_tier(self, current: EscalationTier) -> EscalationTier | None:
        """Return the next tier after current, or None if at expensive_frontier."""
        ordered = sorted(EscalationTier)
        idx = ordered.index(current)
        if idx + 1 >= len(ordered):
            return None
        return ordered[idx + 1]


__all__: list[str] = [
    "EscalationTier",
    "ModelEscalationChain",
    "ModelEscalationLevel",
]
