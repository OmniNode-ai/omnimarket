# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for node_state_persist_effect.

Receives a ModelPersistStateIntent (carried via ModelStatePersistInput),
writes the envelope to ProtocolStateStore, and returns ModelStatePersistedEvent.

This is an EFFECT handler — all I/O is via protocol-based dependency injection.
In tests, inject a mock state store; in production, inject the concrete adapter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from omnibase_core.protocols.storage.protocol_state_store import ProtocolStateStore

from omnimarket.nodes.node_state_persist_effect.models.model_state_persisted_event import (
    ModelStatePersistedEvent,
)

logger = logging.getLogger(__name__)


class HandlerStatePersistEffect:
    """Persists a ModelStateEnvelope to ProtocolStateStore.

    Dependencies are injected via constructor for testability.
    When no state_store is injected, all calls succeed as no-ops (dry-run semantics).

    The handler is deliberately simple:
    1. Call state_store.put(envelope)
    2. Return ModelStatePersistedEvent(intent_id, success=True, persisted_at=now)
    3. On any exception: log the error, return success=False with the error message

    Handler I/O contract is defined in this node's ``contract.yaml``
    (``event_bus.subscribe_topics``, ``event_bus.publish_topics``, ``terminal_event``).
    Do not duplicate topic strings here — they drift from the contract.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        state_store: ProtocolStateStore | None = None,
    ) -> None:
        self._state_store = state_store

    async def handle(
        self,
        correlation_id: UUID,
        intent_id: UUID,
        envelope: ModelStateEnvelope,
        emitted_at: datetime,
    ) -> ModelStatePersistedEvent:
        """Persist the envelope and emit a confirmation event.

        Args:
            correlation_id: Distributed tracing ID.
            intent_id: Unique ID of the originating ModelPersistStateIntent.
            envelope: The state snapshot to write.
            emitted_at: Timezone-aware timestamp from the emitting reducer.

        Returns:
            ModelStatePersistedEvent with success=True and persisted_at set,
            or success=False with error message on failure.
        """
        if self._state_store is None:
            # No store injected — no-op success (allows dry-run / standalone use)
            logger.debug(
                "state_store not injected — no-op persist for intent_id=%s",
                intent_id,
            )
            return ModelStatePersistedEvent(
                intent_id=intent_id,
                success=True,
                persisted_at=datetime.now(UTC),
                error=None,
            )

        try:
            await self._state_store.put(envelope)
            persisted_at = datetime.now(UTC)
            logger.info(
                "State persisted: node_id=%s scope_id=%s intent_id=%s",
                envelope.node_id,
                envelope.scope_id,
                intent_id,
            )
            return ModelStatePersistedEvent(
                intent_id=intent_id,
                success=True,
                persisted_at=persisted_at,
                error=None,
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.error(
                "State persist failed: intent_id=%s node_id=%s error=%s",
                intent_id,
                envelope.node_id,
                error_msg,
            )
            return ModelStatePersistedEvent(
                intent_id=intent_id,
                success=False,
                persisted_at=None,
                error=error_msg,
            )
