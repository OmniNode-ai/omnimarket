# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Joinable per-session current-context state and its read view (OMN-21090, TA.13 step 5)."""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    TranscriptRecordId,
)
from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    ModelActivationRetirement,
    ModelInstallConfirmResult,
)


@unique
class EnumCurrentContextState(StrEnum):
    """Whether the session has a single resolved current boundary."""

    CURRENT = "current"
    NO_BOUNDARY = "no_boundary"
    CONFLICT = "conflict"


class ModelCurrentContextBoundaryObservation(BaseModel):
    """One boundary reading and its installation evidence in the same result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_uuid: TranscriptRecordId
    kind: EnumBoundaryKind
    activation_id: UUID | None = None
    installed: bool | None = None


class ModelSessionCurrentContextState(BaseModel):
    """Stored maximal chains and distinct observations, in deterministic order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: SessionId
    chains: tuple[tuple[TranscriptRecordId, ...], ...] = ()
    observations: tuple[ModelCurrentContextBoundaryObservation, ...] = ()


class ModelSessionCurrentContext(BaseModel):
    """Resolved current boundary and retirements, or explicit conflicts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: SessionId
    state: EnumCurrentContextState
    boundary_uuid: TranscriptRecordId | None = None
    kind: EnumBoundaryKind | None = None
    activation_id: UUID | None = None
    chain_boundary_uuids: tuple[TranscriptRecordId, ...] = ()
    retirements: tuple[ModelActivationRetirement, ...] = ()
    conflicts: tuple[str, ...] = ()


class ModelCurrentContextProjectionRequest(BaseModel):
    """Install-confirm results and optional stored state for one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelInstallConfirmResult, ...] = Field(min_length=1)
    previous: ModelSessionCurrentContextState | None = None

    @model_validator(mode="after")
    def _coherent_session(self) -> Self:
        session_id = self.events[0].session_id
        if any(event.session_id != session_id for event in self.events):
            raise ValueError("install-confirm results must share one session")
        if self.previous is not None and self.previous.session_id != session_id:
            raise ValueError("stored state must belong to the events' session")
        return self


class ModelCurrentContextProjectionResult(BaseModel):
    """Joined state, its current view, and whether the stored state changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelSessionCurrentContextState
    current: ModelSessionCurrentContext
    changed: bool


__all__ = [
    "EnumCurrentContextState",
    "ModelCurrentContextBoundaryObservation",
    "ModelCurrentContextProjectionRequest",
    "ModelCurrentContextProjectionResult",
    "ModelSessionCurrentContext",
    "ModelSessionCurrentContextState",
]
