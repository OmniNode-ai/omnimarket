# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install-confirm trigger input, reader snapshot and outcome (OMN-21115, TA.13)."""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_install_confirm import (
    ModelInstallConfirmRequest,
    ModelInstallConfirmResult,
    ModelPublishedActivation,
)
from omnimarket.models.context_install_confirm_request import ModelCapturedCompactChunk


@unique
class EnumInstallConfirmTriggerOutcome(StrEnum):
    """Whether the applied session content produced a publishable confirmation."""

    NOT_APPLIED = "not_applied"
    SESSION_UNRESOLVED = "session_unresolved"
    NO_COMPACT_RECORDS = "no_compact_records"
    REQUEST_READY = "request_ready"
    ALL_PACKAGES_UNREADABLE = "all_packages_unreadable"


class ModelSessionContentAppliedEvent(BaseModel):
    """The session-content projection's applied event: the row key and the rows written.

    The runtime publishes every projection terminal event with the handler's own
    result plus the acknowledgement key ``projected`` set to ``True`` (OMN-16875),
    so a payload read off the topic carries exactly ``event_id``, ``rows_upserted``
    and ``projected``. The model was refused that payload for the unknown key and
    the runtime sent every applied event to the dead-letter topic (OMN-22478).
    ``projected`` is declared as the one runtime acknowledgement; any other key is
    still refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    rows_upserted: int = Field(ge=0)
    projected: Literal[True] = True


class ModelSessionCompactSnapshot(BaseModel):
    """Captured compact chunks and published activations read for one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    chunks: tuple[ModelCapturedCompactChunk, ...] = ()
    activations: tuple[ModelPublishedActivation, ...] = ()


class ModelInstallConfirmSkipped(BaseModel):
    """The trigger found no ``ProtocolSessionCompactSource`` injected, so it read nothing.

    Published on its own contract-declared topic, never on the completed topic the
    receipts projection folds, so a deployment without a source records the skip
    instead of dead-lettering the applied event (OMN-22478).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    reason: Literal["source_unavailable"] = "source_unavailable"
    dependency: Literal["ProtocolSessionCompactSource"] = "ProtocolSessionCompactSource"


class ModelInstallConfirmTriggerResult(BaseModel):
    """Only ``request_ready`` carries a confirmation for the completed-event topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumInstallConfirmTriggerOutcome
    request: ModelInstallConfirmRequest | None = None
    confirmation: ModelInstallConfirmResult | None = None
    packages_unreadable: tuple[str, ...] = ()


__all__ = [
    "EnumInstallConfirmTriggerOutcome",
    "ModelInstallConfirmSkipped",
    "ModelInstallConfirmTriggerResult",
    "ModelSessionCompactSnapshot",
    "ModelSessionContentAppliedEvent",
]
