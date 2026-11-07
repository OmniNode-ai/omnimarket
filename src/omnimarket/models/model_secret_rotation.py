# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of node_secret_rotation_readback_effect, piece 5's rotation readback (OMN-22913).

After a rotation each consumer of the overlay's list resolves its key from the store and
completes one call with the key it holds. A consumer left on the old key is rejected and reads
NOT WORKING by name. No model holds a secret value, a digest of one or a length.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.models.model_secret_reading import EnumSecretProbeOutcome


class EnumConsumerCallOutcome(StrEnum):
    """How one consumer's call with the key it holds ended. Only ``completed`` passes."""

    COMPLETED = "completed"
    #: The service refused the key the consumer holds: it is still on the old key.
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"
    FAILED = "failed"

    @property
    def verdict(self) -> EnumReadingVerdict:
        if self is EnumConsumerCallOutcome.COMPLETED:
            return EnumReadingVerdict.WORKING
        return EnumReadingVerdict.NOT_WORKING


class ModelRotationReadbackCommand(BaseModel):
    """Read back every consumer after the named key was rotated, approved by a named person.

    ``approver`` has no default meaning: blank, absent or a placeholder is refused by the
    handler with a typed event, never written as a row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    rotation_id: str = Field(min_length=1)
    rotated_folder: str = Field(min_length=1, pattern=r"^/")
    rotated_key: str = Field(min_length=1)
    approver: str = ""
    #: A drill rotates a dev-only key on purpose; its rows are tagged so no operational read counts them.
    drill: bool = False
    write: bool = False


class ModelRotationConsumerReadback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str
    folder: str
    key: str
    resolve: EnumSecretProbeOutcome
    #: None when the key did not resolve, so no call was made.
    call: EnumConsumerCallOutcome | None = None
    verdict: EnumReadingVerdict

    @property
    def failed(self) -> bool:
        return self.verdict is not EnumReadingVerdict.WORKING


class ModelRotationReadbackResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "refused", "failed"]
    verdict: EnumReadingVerdict | None = None
    outcome: EnumReadingOutcome | None = None
    reasons: tuple[str, ...] = ()
    consumers: tuple[ModelRotationConsumerReadback, ...] = ()
    rows: tuple[str, ...] = ()
    ledger: str = "not-written"
    failure: str | None = None


__all__ = [
    "EnumConsumerCallOutcome",
    "ModelRotationConsumerReadback",
    "ModelRotationReadbackCommand",
    "ModelRotationReadbackResult",
]
