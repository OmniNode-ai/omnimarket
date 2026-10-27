# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One client exchange, preserving core envelopes and UUID correlation."""

from uuid import UUID

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool

from omnimarket.models.claude_code_binding import EnumClientMaxMode


class ModelDelegationReceipt(BaseModel):
    """The transport topic and the unmodified core event envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    topic: str = Field(min_length=1)
    envelope: ModelEventEnvelope[dict[str, JsonValue]]


class ModelReceiptVerdictRequest(BaseModel):
    """Local call identity and the receipts observed by its transport."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    client_call_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_max_mode: EnumClientMaxMode
    published: StrictBool
    receipts: tuple[ModelDelegationReceipt, ...] = ()
