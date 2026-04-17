# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event emitted by node_state_persist_effect after a persistence attempt."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelStatePersistedEvent(BaseModel):
    """Confirmation event published to onex.evt.state.persisted.v1.

    Emitted by HandlerStatePersistEffect after calling ProtocolStateStore.put().
    Consumers (e.g. HandlerLedgerStateReducer) can use this to confirm that
    their emitted intent was fulfilled.

    Attributes:
        intent_id: Echoes the intent_id from the originating ModelPersistStateIntent.
        success: True if ProtocolStateStore.put() completed without error.
        persisted_at: Timezone-aware timestamp of successful write, else None.
        error: Error message on failure, else None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    intent_id: UUID = Field(
        ..., description="Echo of the originating intent_id for correlation."
    )
    success: bool = Field(..., description="True if state was persisted successfully.")
    persisted_at: datetime | None = Field(
        default=None,
        description="Timestamp of successful persistence, or None on failure.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if success=False, else None.",
    )
