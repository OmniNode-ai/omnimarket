# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One client-call claim row (plan TB.3, OMN-21098)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegate_skill_orchestrator.models.enum_client_call_claim_state import (
    EnumClientCallClaimState,
)

_SHA256_HEX = r"^[0-9a-f]{64}$"


class ModelClientCallClaimRow(BaseModel):
    """The control state of one client call, keyed by ``client_call_key``.

    ``generation`` only rises. Every write after the claim is guarded on the
    generation its writer holds, so a writer whose lease was taken over changes
    nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_call_key: str = Field(..., pattern=_SHA256_HEX)
    request_fingerprint: str = Field(..., pattern=_SHA256_HEX)
    state: EnumClientCallClaimState
    generation: int = Field(..., ge=1)
    owner_id: str = Field(..., min_length=1)
    lease_expires_at: datetime
    effective_mode: str | None = Field(
        default=None, description="Mode recorded by the admission commit."
    )
    admitted: bool | None = Field(
        default=None, description="Admission verdict recorded by the admission commit."
    )
    terminal_json: str = Field(
        default="", description="The stored terminal, empty until one is stored."
    )
