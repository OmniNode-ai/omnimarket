# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed census event, group state record and reduce command of a bus consumer group (OMN-23992).

One bus lane's consumer groups are held in one record. The guard effect publishes one
``ModelGroupCensusEvent`` per tick; the reducer folds each census into the record and the record is the
projection every reader uses. No file is needed: the record carries ``empty_since`` per group.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelGroupCensusEvent(BaseModel):
    """One census of the consumer groups of one bus lane, as published on the census topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    bus_lane: str = Field(min_length=1)
    observed_at: AwareDatetime
    groups: dict[str, str] = Field(
        min_length=1,
        description="group name -> UPPER-CASE state such as EMPTY, STABLE",
    )


class ModelGroupState(BaseModel):
    """The state of one consumer group within the record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    state: str = Field(min_length=1)
    empty_since: AwareDatetime | None = None
    last_seen: AwareDatetime


class ModelGroupStateRecord(BaseModel):
    """The projection: the state of every consumer group of one bus lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bus_lane: str = Field(min_length=1)
    updated_at: AwareDatetime
    census_correlation_id: UUID
    groups: tuple[ModelGroupState, ...] = ()
    reap_due: tuple[str, ...] = ()
    decided_by: Literal["reducer"] = "reducer"
    decision_digest: str = Field(min_length=64, max_length=64)


class ModelGroupStateReduceCommand(BaseModel):
    """The prior record of the bus lane (absent for the first census) and the census to fold into it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prior: ModelGroupStateRecord | None = None
    event: ModelGroupCensusEvent


def decision_digest(
    bus_lane: str,
    census_correlation_id: UUID,
    reap_due: tuple[str, ...],
    empty_since_by_name: Mapping[str, datetime],
    observed_at: datetime,
) -> str:
    """sha256 hex of the canonical json of the reap decision.

    The canonical form covers the bus lane, the census, the census time and the due names with their empty-since times; the reap effect recomputes this to prove a command came
    from a reducer record.
    """
    payload = {
        "bus_lane": bus_lane,
        "census": str(census_correlation_id),
        "observed_at": observed_at.isoformat(),
        "due": sorted(reap_due),
        "since": {
            name: empty_since_by_name[name].isoformat() for name in sorted(reap_due)
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__: list[str] = [
    "ModelGroupCensusEvent",
    "ModelGroupState",
    "ModelGroupStateRecord",
    "ModelGroupStateReduceCommand",
    "decision_digest",
]
