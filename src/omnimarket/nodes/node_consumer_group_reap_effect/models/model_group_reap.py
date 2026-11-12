# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed reap command, refusal and outcome for a bus consumer group (OMN-23992).

The command is built from a reducer record and proves it was decided by the reducer through a
recomputed decision digest; a command decided from a file is refused and deletes nothing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_consumer_group_state_reducer.models.model_group_state import (
    ModelGroupStateRecord,
)


class ModelDueGroup(BaseModel):
    """One consumer group the reducer named as due for reaping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    empty_since: AwareDatetime


class ModelGroupReapCommand(BaseModel):
    """A typed reap command that proves it was decided by the reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    bus_lane: str = Field(min_length=1)
    decided_by: str
    census_correlation_id: UUID
    observed_at: AwareDatetime
    groups: tuple[ModelDueGroup, ...]
    decision_digest: str

    @classmethod
    def from_state_record(
        cls, record: ModelGroupStateRecord, correlation_id: UUID
    ) -> ModelGroupReapCommand:
        """Build the command from a reducer record: the due names with their empty_since."""
        empty_since = {
            g.name: g.empty_since for g in record.groups if g.empty_since is not None
        }
        groups = tuple(
            ModelDueGroup(name=name, empty_since=empty_since[name])
            for name in record.reap_due
        )
        return cls(
            correlation_id=correlation_id,
            bus_lane=record.bus_lane,
            decided_by="reducer",
            census_correlation_id=record.census_correlation_id,
            observed_at=record.updated_at,
            groups=groups,
            decision_digest=record.decision_digest,
        )


class EnumReapRefusal(StrEnum):
    """The typed reasons a reap command is refused, or a group is dropped."""

    DECIDED_NOT_BY_REDUCER = "DECIDED_NOT_BY_REDUCER"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    BOUND_NOT_MET = "BOUND_NOT_MET"
    NOT_REAPABLE = "NOT_REAPABLE"
    PROTECTED = "PROTECTED"
    OVER_CAP = "OVER_CAP"
    DISABLED = "DISABLED"
    BROKER_UNHEALTHY = "BROKER_UNHEALTHY"


class ModelGroupReapRefusal(BaseModel):
    """One typed refusal; the group is empty when the refusal concerns the whole command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: EnumReapRefusal
    group: str = ""
    detail: str = ""


class ModelGroupReapOutcome(BaseModel):
    """The result of a reap run: its status, refusals, deletes and the read-back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    bus_lane: str
    status: Literal["refused", "dry_run", "reaped", "partial", "failed"]
    refusals: tuple[ModelGroupReapRefusal, ...] = ()
    would_delete: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    delete_errors: dict[str, str] = {}
    read_back_absent: tuple[str, ...] = ()
    read_back_present: tuple[str, ...] = ()
    detail: str = ""


__all__: list[str] = [
    "EnumReapRefusal",
    "ModelDueGroup",
    "ModelGroupReapCommand",
    "ModelGroupReapOutcome",
    "ModelGroupReapRefusal",
]
