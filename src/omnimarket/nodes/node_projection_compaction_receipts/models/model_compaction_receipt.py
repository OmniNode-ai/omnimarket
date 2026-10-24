# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection-owned rows; the producer's result stays in shared models."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_compaction.model_context_compaction_fields import (
    SessionId,
    Sha256Hex,
    TranscriptRecordId,
)
from omnimarket.models.context_install_confirm import (
    EnumBoundaryKind,
    EnumInstallFact,
    EnumInstallFactReason,
    ModelBoundaryReading,
    ModelInstallConfirmResult,
)


class ModelCompactionReceipt(BaseModel):
    """One boundary, or an activation whose boundary was not captured yet."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: SessionId
    receipt_key: str
    boundary_uuid: TranscriptRecordId | None
    boundary_kind: EnumBoundaryKind | None
    activation_id: UUID | None
    verdict: EnumInstallFact
    reason: EnumInstallFactReason | None = None
    installed_messages_hash: Sha256Hex | None = None
    observed_hash: Sha256Hex | None = None

    @model_validator(mode="after")
    def _coherent_verdict(self) -> Self:
        if self.boundary_uuid is None:
            if (
                self.activation_id is None
                or self.boundary_kind is not None
                or self.verdict is not EnumInstallFact.UNCERTAIN
                or self.reason is not EnumInstallFactReason.CAPTURE_GAP
            ):
                raise ValueError(
                    "a boundary-free receipt must be an activation capture gap"
                )
            expected_key = f"activation:{self.activation_id}"
        else:
            expected_key = f"boundary:{self.boundary_uuid}"
            if self.boundary_kind is None:
                raise ValueError("a captured boundary must retain its kind")
        if self.receipt_key != expected_key:
            raise ValueError("receipt key disagrees with its boundary or activation")
        if self.verdict in {
            EnumInstallFact.INSTALLED,
            EnumInstallFact.INSTALL_MISMATCH,
        }:
            if (
                self.boundary_kind is not EnumBoundaryKind.CUSTOM
                or self.activation_id is None
                or self.installed_messages_hash is None
                or self.observed_hash is None
                or self.reason is not None
            ):
                raise ValueError(
                    "a decided install needs its custom boundary and both hashes"
                )
            equal = self.installed_messages_hash == self.observed_hash
            if equal != (self.verdict is EnumInstallFact.INSTALLED):
                raise ValueError("install verdict disagrees with the measured hashes")
        elif self.verdict is EnumInstallFact.NOT_INSTALLED:
            if self.reason not in {
                EnumInstallFactReason.NATIVE_COMPACTION,
                EnumInstallFactReason.NOT_SELECTED,
            }:
                raise ValueError("not_installed needs the compute's non-install reason")
        elif self.reason not in {
            EnumInstallFactReason.CAPTURE_GAP,
            EnumInstallFactReason.UNPARSEABLE_RECORD,
        }:
            raise ValueError("uncertain needs the compute's uncertainty reason")
        return self


class ModelCompactionReceiptsProjectionRequest(BaseModel):
    """The producer's completed result and the stored rows for its session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: ModelInstallConfirmResult
    previous_rows: tuple[ModelCompactionReceipt, ...] = ()

    @model_validator(mode="after")
    def _coherent_event(self) -> Self:
        boundaries: dict[str, ModelBoundaryReading] = {}
        for reading in self.event.boundaries:
            if (
                reading.boundary_uuid in boundaries
                and boundaries[reading.boundary_uuid] != reading
            ):
                raise ValueError("conflicting copies of a compaction boundary")
            boundaries[reading.boundary_uuid] = reading
        facts = {fact.activation_id: fact for fact in self.event.facts}
        if len(facts) != len(self.event.facts):
            raise ValueError("duplicate installation facts for an activation")
        for fact in self.event.facts:
            boundary = boundaries.get(fact.boundary_uuid or "")
            if fact.boundary_uuid is not None and boundary is None:
                raise ValueError("installation fact references an absent boundary")
            if fact.fact in {
                EnumInstallFact.INSTALLED,
                EnumInstallFact.INSTALL_MISMATCH,
            } and (boundary is None or boundary.activation_id != fact.activation_id):
                raise ValueError(
                    "installation fact does not name the selected activation"
                )
            if (
                boundary is not None
                and boundary.activation_id == fact.activation_id
                and boundary.observed_hash != fact.observed_hash
            ):
                raise ValueError(
                    "selected fact and boundary disagree on the observed hash"
                )
            # Validate even non-selected facts. A malformed sibling fact must
            # refuse the whole event before any of its boundary rows are written.
            ModelCompactionReceipt(
                session_id=self.event.session_id,
                receipt_key=(
                    f"boundary:{fact.boundary_uuid}"
                    if fact.boundary_uuid is not None
                    else f"activation:{fact.activation_id}"
                ),
                boundary_uuid=fact.boundary_uuid,
                boundary_kind=boundary.kind if boundary is not None else None,
                activation_id=fact.activation_id,
                verdict=fact.fact,
                reason=fact.reason,
                installed_messages_hash=fact.installed_messages_hash,
                observed_hash=fact.observed_hash,
            )
        return self


class ModelCompactionReceiptsProjectionResult(BaseModel):
    """Rows to persist; pending activations never fabricate receipts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelCompactionReceipt, ...]


__all__ = [
    "ModelCompactionReceipt",
    "ModelCompactionReceiptsProjectionRequest",
    "ModelCompactionReceiptsProjectionResult",
]
