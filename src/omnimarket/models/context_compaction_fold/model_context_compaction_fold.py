# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable request and result for the compaction receipts fold (OMN-17427, TA.13)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.context_compaction import (
    EnumContextCompactionFailureClass,
    EnumContextInstallUncertainReason,
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionInstalled,
    ModelContextCompactionInstallMismatch,
    ModelContextCompactionInstallUncertain,
    ModelContextCompactionPrepared,
)
from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    Sha256Hex,
    TranscriptRecordId,
)


class EnumCompactionCandidateState(StrEnum):
    PREPARED = "prepared"
    CONFLICT = "conflict"
    UNPREPARED = "unprepared"


class EnumCompactionActivationState(StrEnum):
    ACTIVATED = "activated"
    INSTALLED = "installed"
    INSTALL_MISMATCH = "install_mismatch"
    NOT_INSTALLED = "not_installed"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"


class ModelCompactionFoldRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prepared: tuple[ModelContextCompactionPrepared, ...] = ()
    activated: tuple[ModelContextCompactionActivated, ...] = ()
    fallback: tuple[ModelContextCompactionFallback, ...] = ()
    installed: tuple[ModelContextCompactionInstalled, ...] = ()
    install_mismatch: tuple[ModelContextCompactionInstallMismatch, ...] = ()
    install_uncertain: tuple[ModelContextCompactionInstallUncertain, ...] = ()


class ModelCompactionCandidateFold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    session_id: SessionId | None
    state: EnumCompactionCandidateState
    assembly_hashes: tuple[str, ...]
    activation_ids: tuple[UUID, ...]
    fallback_invocation_ids: tuple[UUID, ...]


class ModelCompactionActivationFold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    activation_id: UUID
    session_id: SessionId
    candidate_id: UUID | None
    activated: bool
    state: EnumCompactionActivationState
    reason: EnumContextInstallUncertainReason | None = None
    boundary_uuid: TranscriptRecordId | None = None
    observed_hash: Sha256Hex | None = None
    conflicts: tuple[str, ...] = ()


class ModelCompactionFallbackFold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    session_id: SessionId
    candidate_id: UUID | None
    failure_classes: tuple[EnumContextCompactionFailureClass, ...]
    conflict: bool


class ModelCompactionFoldResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[ModelCompactionCandidateFold, ...]
    activations: tuple[ModelCompactionActivationFold, ...]
    fallbacks: tuple[ModelCompactionFallbackFold, ...]


__all__ = [
    "EnumCompactionActivationState",
    "EnumCompactionCandidateState",
    "ModelCompactionActivationFold",
    "ModelCompactionCandidateFold",
    "ModelCompactionFallbackFold",
    "ModelCompactionFoldRequest",
    "ModelCompactionFoldResult",
]
