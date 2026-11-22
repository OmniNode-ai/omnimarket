# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the topic reaper (OMN-21594, hot-loading plan task A6).

Nothing here has a default for a policy value: the two intervals, the quiet-observation count and the
enable flag come from the lane overlay, and a request that carries no policy deletes nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

REAPER_POLICY_MISSING = "REAPER_POLICY_MISSING"


class EnumReaperState(StrEnum):
    """The states of ``topic_reaper_fsm`` (hot-loading plan 3.11.9)."""

    ACTIVE = "active"
    FLAGGED = "flagged"
    ELIGIBLE = "eligible"
    DELETING = "deleting"
    DELETED = "deleted"


class ModelTopicReaperPolicy(BaseModel):
    """The per-lane reaper policy, every value declared in the lane overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A topic with no produce and no committed consume for this long is flagged.
    quiet_days_before_flag: int = Field(ge=1)
    #: A flag stands this long, with the quiet observations below, before the topic is deletable.
    flag_to_delete_days: int = Field(ge=1)
    #: Observations that must show zero group members and unmoved end offsets before a delete.
    quiet_observations_required: int = Field(ge=1)
    #: Whether this lane lets the reaper hand out delete targets; false reports only.
    enforce: bool
    #: Topic name prefixes that are never flagged (broker-internal, baseline, control).
    protected_prefixes: tuple[str, ...]
    #: Exact topic names that are baseline or control topics and never flagged.
    baseline_topics: tuple[str, ...]


class ModelReaperObservation(BaseModel):
    """What the broker says about one topic at the observation time.

    ``None`` in an evidence field means the field was not read; an unread topic is held where it is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    partitions: int = Field(ge=1)
    topic_id: str | None = None
    #: Sum of the partitions' end offsets.
    end_offset_total: int | None
    #: Timestamp of the newest record, when read.
    last_record_at: datetime | None = None
    #: Live members across every group that subscribes to the topic.
    group_members: int | None
    #: Groups holding a committed offset on the topic.
    committed_groups: tuple[str, ...] | None
    #: Newest commit time over those groups; ``None`` with committed groups means it was not read.
    last_commit_at: datetime | None = None
    #: When the topic was created, or the nearest evidence of it (its on-disk directory's birth time).
    #: It starts the quiet clock of a topic that has never had a record or a commit.
    created_at: datetime | None = None


class ModelReaperTopicState(BaseModel):
    """The reaper's record of one topic, handed back in on the next observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    state: EnumReaperState
    last_activity_at: datetime
    last_end_offset_total: int | None
    flagged_at: datetime | None = None
    quiet_observations: int = Field(default=0, ge=0)
    topic_id: str | None = None


class ModelTopicReaperRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The injected clock: every age is measured against this, never the wall clock.
    now: datetime
    #: ``None`` when the lane overlay holds no reaper block.
    policy: ModelTopicReaperPolicy | None
    observations: tuple[ModelReaperObservation, ...]
    state: tuple[ModelReaperTopicState, ...] = ()
    #: Topics the lane's boot-set contracts subscribe to or publish; never flagged.
    boot_set_topics: tuple[str, ...]
    #: Topics whose contract announced itself again since the last observation.
    reannounced_topics: tuple[str, ...] = ()
    #: Topics stored state or a pending emission of an older package generation still names.
    unfinished_work_topics: tuple[str, ...] = ()

    @field_validator("now")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("now must carry a timezone")
        return value


class ModelReaperDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    prior_state: EnumReaperState | None
    state: EnumReaperState | None
    partitions: int = Field(ge=0)
    #: Why the topic stayed where it was or how it moved (a short typed code).
    reason: str


class ModelReaperDeleteTarget(BaseModel):
    """One topic to delete, named with the id the reaper recorded for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    topic_id: str
    partitions: int = Field(ge=1)


class ModelTopicReaperResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["observed", "policy_missing"]
    #: ``REAPER_POLICY_MISSING`` when the request held no policy, else ``None``.
    report_code: str | None = None
    state: tuple[ModelReaperTopicState, ...] = ()
    decisions: tuple[ModelReaperDecision, ...] = ()
    #: Delete targets handed out; empty unless the policy enforces.
    delete_targets: tuple[ModelReaperDeleteTarget, ...] = ()
    #: Counts by state after this observation.
    flagged_topics: int = 0
    deletable_topics: int = 0
    deletable_partitions: int = 0
    #: Deletable topics whose id was not read; they are never handed out for deletion.
    deletable_without_topic_id: int = 0
    #: Topics held with unread evidence.
    held_unread: int = 0
