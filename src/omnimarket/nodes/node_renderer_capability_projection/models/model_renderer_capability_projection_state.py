# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Renderer Capability Registry projection state + rows (OMN-13131 / W5).

These models are the *read authority* for renderer capabilities. There is no
``CapabilityRegistry`` class, no in-memory registry authority, and no UI-owned
registry state: the materialized projection (read via ``/projection/{topic}``)
is the only place a consumer learns what a renderer can render.

Freshness model
---------------
Each row carries ``declared_at`` (when the renderer last heartbeated) and
``last_heartbeat`` (an alias kept for the projection schema/dashboard). A row is
``is_degraded`` when the heartbeat is older than the TTL relative to an observer
clock — the projection *expresses* degradation rather than silently rendering a
stale capability. When a row is degraded (or absent), the typed empty-state
reason is ``EnumEmptyStateReason.UPSTREAM_BLOCKED`` so the client surfaces the
gate instead of rendering blind.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnibase_core.enums.enum_accessibility_tier import EnumAccessibilityTier
from omnibase_core.enums.enum_empty_state_reason import EnumEmptyStateReason
from omnibase_core.enums.enum_renderer_interaction_model import (
    EnumRendererInteractionModel,
)
from omnibase_core.enums.enum_widget_type import EnumWidgetType
from omnibase_core.models.primitives.model_semver import ModelSemVer
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_HEARTBEAT_TTL_SECONDS",
    "ModelRendererCapabilityProjectionRow",
    "ModelRendererCapabilityProjectionState",
]

# Default heartbeat freshness window. A renderer that has not re-declared within
# this many seconds is treated as degraded. Kept as a module constant (not a
# magic number at the call site) so the TTL is a single, auditable authority.
DEFAULT_HEARTBEAT_TTL_SECONDS: int = 90


class ModelRendererCapabilityProjectionRow(BaseModel):
    """One renderer's materialized capability projection row.

    Carries the full projection schema declared in the W5 plan
    (``renderer_id``, ``platform``, ``supported_component_kinds``,
    ``interaction_model``, ``accessibility_tier``, ``contract_version``,
    ``declared_at``, ``last_heartbeat``) plus the derived freshness fields the
    projection exposes so the client never has to recompute TTL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    renderer_id: str = Field(  # string-id-ok: human-readable renderer label, not a UUID
        ...,
        description="Stable renderer identifier (e.g. 'ui.effect.web')",
        min_length=1,
    )
    platform: str = Field(
        ..., description="Target platform the renderer runs on", min_length=1
    )
    supported_component_kinds: tuple[EnumWidgetType, ...] = Field(
        ..., description="Component kinds this renderer can render"
    )
    interaction_model: EnumRendererInteractionModel = Field(
        ..., description="Interaction model the renderer advertises"
    )
    accessibility_tier: EnumAccessibilityTier = Field(
        ..., description="WCAG-aligned accessibility tier the renderer guarantees"
    )
    contract_version: ModelSemVer = Field(
        ..., description="Semantic version of the capability contract this row declares"
    )
    declared_at: datetime = Field(
        ..., description="Heartbeat instant the renderer last declared this capability"
    )
    last_heartbeat: datetime = Field(
        ...,
        description="Most recent heartbeat instant (freshness anchor; mirrors declared_at)",
    )
    is_degraded: bool = Field(
        ...,
        description="True when the heartbeat is older than the TTL at the observer clock",
    )
    empty_state_reason: EnumEmptyStateReason | None = Field(
        default=None,
        description=(
            "Typed reason a consumer must surface instead of rendering this row. "
            "UPSTREAM_BLOCKED when degraded; None when fresh."
        ),
    )

    def freshness_at(
        self, observed_at: datetime, ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS
    ) -> ModelRendererCapabilityProjectionRow:
        """Return a copy of this row with freshness re-derived at ``observed_at``.

        Pure: re-evaluates ``is_degraded`` and ``empty_state_reason`` against the
        observer clock without mutating the heartbeat data. The projection host
        calls this so a read reflects current TTL even between declarations.
        """
        degraded = is_heartbeat_degraded(
            last_heartbeat=self.last_heartbeat,
            observed_at=observed_at,
            ttl_seconds=ttl_seconds,
        )
        return self.model_copy(
            update={
                "is_degraded": degraded,
                "empty_state_reason": (
                    EnumEmptyStateReason.UPSTREAM_BLOCKED if degraded else None
                ),
            }
        )


class ModelRendererCapabilityProjectionState(BaseModel):
    """Reducer-owned projection state: one row per ``renderer_id``.

    The reducer is the sole writer; the state is a pure fold of declaration
    heartbeats. ``rows`` is keyed by ``renderer_id`` so a later heartbeat from
    the same renderer replaces (not duplicates) the prior row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    rows: tuple[ModelRendererCapabilityProjectionRow, ...] = Field(
        default=(),
        description="Materialized capability rows, one per renderer_id",
    )

    def row_for(self, renderer_id: str) -> ModelRendererCapabilityProjectionRow | None:
        """Return the row for ``renderer_id`` or None if no renderer declared it."""
        return next((r for r in self.rows if r.renderer_id == renderer_id), None)


def is_heartbeat_degraded(
    *,
    last_heartbeat: datetime,
    observed_at: datetime,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> bool:
    """Pure TTL predicate: True when ``last_heartbeat`` is older than the TTL.

    A heartbeat exactly at the TTL boundary is still fresh (strictly-greater
    comparison); only a gap that *exceeds* the TTL is degraded.
    """
    return observed_at - last_heartbeat > timedelta(seconds=ttl_seconds)
