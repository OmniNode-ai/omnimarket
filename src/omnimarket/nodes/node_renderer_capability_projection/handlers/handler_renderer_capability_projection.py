# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Renderer Capability Registry projection REDUCER handler (OMN-13131 / W5).

Canonical REDUCER node: a pure fold ``(state, declaration) -> new_state``. The
runtime delivers a ``ModelEventEnvelope`` whose payload is a
``ModelRendererCapabilityDeclaration`` (a renderer capability heartbeat
published on the renderer-capability-declared command topic — constant
``RENDERER_CAPABILITY_DECLARED_TOPIC_V1`` in ``omnimarket.events.topics``); the
handler returns ``ModelHandlerOutput.for_reducer`` carrying the advanced
projection state as its sole projection.

This node is the **sole writer** of the Renderer Capability Registry. The
registry is a *projection*, not a class — there is no ``CapabilityRegistry``
object, no in-memory authority, and no UI-owned state. Consumers read the
materialized projection via ``/projection/{topic}``; a stale/absent renderer
surfaces ``EnumEmptyStateReason.UPSTREAM_BLOCKED`` rather than rendering blind.

No I/O, no bus, no DB: the handler folds and emits a projection; the effects
runtime materializes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_declaration import (
    ModelRendererCapabilityDeclaration,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_projection_state import (
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    ModelRendererCapabilityProjectionState,
)
from omnimarket.nodes.node_renderer_capability_projection.renderer_capability_fold import (
    fold_declaration,
)

HANDLER_ID = "renderer-capability-projection-reducer"

# Reducer state key carried alongside the envelope payload so the pure fold
# accumulates across dispatch invocations. The runtime/projection host supplies
# prior state under this key; absent, the fold starts from an empty projection.
STATE_KEY = "_state"


class HandlerRendererCapabilityProjection:
    """Pure reducer: fold one renderer capability heartbeat into the projection."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Fold one declaration heartbeat and emit the new projection state."""
        declaration, prior_state = _coerce(envelope.payload)
        observed_at = _observed_at(envelope)
        new_state = fold_declaration(
            prior_state,
            declaration,
            observed_at=observed_at,
            ttl_seconds=DEFAULT_HEARTBEAT_TTL_SECONDS,
        )
        return ModelHandlerOutput.for_reducer(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            projections=(new_state,),
        )


def _observed_at(envelope: ModelEventEnvelope[Any]) -> datetime:
    """Observer clock for freshness: the envelope timestamp (already tz-aware)."""
    ts = envelope.envelope_timestamp
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _coerce(
    payload: Any,
) -> tuple[ModelRendererCapabilityDeclaration, ModelRendererCapabilityProjectionState]:
    """Coerce the dispatched payload into (declaration, prior_state).

    Accepts a typed ``ModelRendererCapabilityDeclaration`` directly, or a mapping
    carrying the declaration fields plus an optional ``_state`` projection the
    fold accumulates onto. A payload that is neither is a hard type error — the
    reducer never silently swallows an unroutable payload.
    """
    if isinstance(payload, ModelRendererCapabilityDeclaration):
        return payload, ModelRendererCapabilityProjectionState()
    if isinstance(payload, Mapping):
        data = dict(payload)
        raw_state = data.pop(STATE_KEY, None)
        prior_state = _coerce_state(raw_state)
        declaration = ModelRendererCapabilityDeclaration.model_validate(data)
        return declaration, prior_state
    if hasattr(payload, "model_dump"):
        return (
            ModelRendererCapabilityDeclaration.model_validate(payload.model_dump()),
            ModelRendererCapabilityProjectionState(),
        )
    raise TypeError(
        "renderer-capability declaration payload must be "
        "ModelRendererCapabilityDeclaration or a mapping; "
        f"got {type(payload).__name__}"
    )


def _coerce_state(raw_state: Any) -> ModelRendererCapabilityProjectionState:
    """Coerce a prior-state value into a projection state (empty when absent)."""
    if raw_state is None:
        return ModelRendererCapabilityProjectionState()
    if isinstance(raw_state, ModelRendererCapabilityProjectionState):
        return raw_state
    if isinstance(raw_state, Mapping):
        return ModelRendererCapabilityProjectionState.model_validate(dict(raw_state))
    raise TypeError(
        "prior projection state must be ModelRendererCapabilityProjectionState or a "
        f"mapping; got {type(raw_state).__name__}"
    )


__all__ = [
    "HANDLER_ID",
    "STATE_KEY",
    "HandlerRendererCapabilityProjection",
]
