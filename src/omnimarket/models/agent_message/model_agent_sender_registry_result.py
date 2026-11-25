# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a registry operation did. The result never carries a private key."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumAgentSenderRegistryOutcome(StrEnum):
    REGISTERED = "registered"
    LISTED = "listed"
    REVOKED = "revoked"
    ALREADY_REGISTERED = "already_registered"
    NOT_REGISTERED = "not_registered"
    STORE_FAILED = "store_failed"


class ModelAgentSenderRegistryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumAgentSenderRegistryOutcome
    sender: str | None = None
    principal: str | None = None
    key_id: str | None = None
    public_key: str | None = None
    senders: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in {
            EnumAgentSenderRegistryOutcome.REGISTERED,
            EnumAgentSenderRegistryOutcome.LISTED,
            EnumAgentSenderRegistryOutcome.REVOKED,
        }


__all__: list[str] = [
    "EnumAgentSenderRegistryOutcome",
    "ModelAgentSenderRegistryResult",
]
