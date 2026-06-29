# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure fold for the Renderer Capability Registry projection (OMN-13131 / W5).

This module is the deterministic core of ``node_renderer_capability_projection``.
It folds one ``ModelRendererCapabilityDeclaration`` heartbeat into the
projection state: ``(state, declaration, observed_at) -> new_state``. No I/O, no
bus, no DB — the handler wraps this fold and the runtime publishes/materializes.

The fold is the only writer of capability rows. It keys on ``renderer_id`` so a
renderer's later heartbeat replaces its prior row (last-write-wins on heartbeat
recency), and it re-derives every other row's freshness against the observer
clock so a state read reflects current TTL.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_declaration import (
    ModelRendererCapabilityDeclaration,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_projection_state import (
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    ModelRendererCapabilityProjectionRow,
    ModelRendererCapabilityProjectionState,
    is_heartbeat_degraded,
)

__all__ = ["fold_declaration", "row_from_declaration"]


def row_from_declaration(
    declaration: ModelRendererCapabilityDeclaration,
    *,
    observed_at: datetime,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> ModelRendererCapabilityProjectionRow:
    """Materialize one projection row from a declaration at ``observed_at``.

    Freshness is derived against the observer clock: a declaration whose
    heartbeat already exceeds the TTL at materialization time is born degraded
    and carries ``EnumEmptyStateReason.UPSTREAM_BLOCKED``.
    """
    cap = declaration.capability
    degraded = is_heartbeat_degraded(
        last_heartbeat=declaration.declared_at,
        observed_at=observed_at,
        ttl_seconds=ttl_seconds,
    )
    row = ModelRendererCapabilityProjectionRow(
        renderer_id=cap.renderer_id,
        platform=cap.platform,
        supported_component_kinds=cap.supported_component_kinds,
        interaction_model=cap.interaction_model,
        accessibility_tier=cap.accessibility_tier,
        contract_version=cap.contract_version,
        declared_at=declaration.declared_at,
        last_heartbeat=declaration.declared_at,
        is_degraded=degraded,
        empty_state_reason=None,
    )
    # Re-derive through the row helper so degraded rows carry the typed reason.
    return row.freshness_at(observed_at, ttl_seconds=ttl_seconds)


def fold_declaration(
    state: ModelRendererCapabilityProjectionState,
    declaration: ModelRendererCapabilityDeclaration,
    *,
    observed_at: datetime,
    ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> ModelRendererCapabilityProjectionState:
    """Fold one declaration heartbeat into the projection state.

    - Upserts the declaring renderer's row (keyed on ``renderer_id``).
    - Re-derives freshness for every *other* row against ``observed_at`` so a
      stale renderer flips to ``is_degraded`` once its TTL lapses, even though
      this fold was triggered by a different renderer's heartbeat.
    """
    new_row = row_from_declaration(
        declaration, observed_at=observed_at, ttl_seconds=ttl_seconds
    )
    refreshed_others = tuple(
        row.freshness_at(observed_at, ttl_seconds=ttl_seconds)
        for row in state.rows
        if row.renderer_id != new_row.renderer_id
    )
    return ModelRendererCapabilityProjectionState(rows=(*refreshed_others, new_row))
