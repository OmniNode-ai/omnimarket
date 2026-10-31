# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable delivery facts and records; lifecycle policy lives in the contract."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ModelDeliveryConfig(BaseModel):
    """Required delivery policy loaded from the node contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_minutes: int = Field(gt=0)
    limit_trigger: str
    decision_state: str
    refused_reference_kinds: tuple[str, ...]


class ModelDeliveryReference(BaseModel):
    """A reference kind and the central validator's verdict for its role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref_kind: str = Field(min_length=1)
    reference_allowed: bool


class ModelDeliveryMerge(BaseModel):
    """A merge that can establish a new expected target and deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merge_commit: str = Field(min_length=1)
    merged_at: AwareDatetime


class ModelDeliveryTag(BaseModel):
    """A tag, the immutable artifact it names and the merge it covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str = Field(min_length=1)
    artifact_identity: str = Field(min_length=1)
    merge: ModelDeliveryMerge


class ModelDeliveryReceipt(BaseModel):
    """Installed target evidence, including its generation and identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str | None = None
    target_generation: int | None = Field(default=None, ge=0)
    artifact_identity: str | None = None


class ModelDeliveryConsumer(BaseModel):
    """The tag a consumer was found on; absence means no tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str | None = None


class ModelDeliveryPathEvent(BaseModel):
    """One caller-supplied trigger and its delivery facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger: str = Field(min_length=1)
    reference: ModelDeliveryReference | None = None
    tag: ModelDeliveryTag | None = None
    merge: ModelDeliveryMerge | None = None
    receipt: ModelDeliveryReceipt | None = None
    consumer: ModelDeliveryConsumer | None = None

    @model_validator(mode="after")
    def reject_tag_and_merge(self) -> Self:
        if self.tag is not None and self.merge is not None:
            raise ValueError("an event cannot carry both tag and merge")
        return self


class ModelDeliveryPathRecord(BaseModel):
    """Persisted target, deadline and once-per-anchor stale decision marker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: str | None = None
    expected_since: AwareDatetime | None = None
    merge_commit: str | None = None
    target_tag: str | None = None
    artifact_identity: str | None = None
    target_generation: int = Field(default=0, ge=0)
    stale_decided_for: AwareDatetime | None = None


class ModelDeliveryPathRequest(BaseModel):
    """A path record, optional event and caller-supplied clock reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str = Field(min_length=1)
    record: ModelDeliveryPathRecord = ModelDeliveryPathRecord()
    event: ModelDeliveryPathEvent | None = None
    now: AwareDatetime


class ModelDeliveryDecisionRow(BaseModel):
    """One entry into the decision state, captured with its target and policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    decision: str
    from_state: str
    expected_since: AwareDatetime | None
    stale_after_minutes: int
    target_tag: str | None
    target_generation: int
    decided_at: AwareDatetime

    @classmethod
    def from_record(
        cls,
        path_id: str,
        from_state: str,
        record: ModelDeliveryPathRecord,
        config: ModelDeliveryConfig,
        decided_at: datetime,
    ) -> Self:
        """Snapshot a decision entry after the transition's record updates."""
        return cls(
            path_id=path_id,
            decision=config.decision_state,
            from_state=from_state,
            expected_since=record.expected_since,
            stale_after_minutes=config.stale_after_minutes,
            target_tag=record.target_tag,
            target_generation=record.target_generation,
            decided_at=decided_at,
        )


class ModelDeliveryPathResult(BaseModel):
    """The next record, accepted triggers and decision rows for one fold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_id: str
    prior_state: str
    state: str
    moved: bool
    accepted: bool
    record: ModelDeliveryPathRecord
    triggers: tuple[str, ...]
    decision_rows: tuple[ModelDeliveryDecisionRow, ...]
    error: str | None = None
    failed_conditions: tuple[str, ...] = ()
    decided_at: AwareDatetime
