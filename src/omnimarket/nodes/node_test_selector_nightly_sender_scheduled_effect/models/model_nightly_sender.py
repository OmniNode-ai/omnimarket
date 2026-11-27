# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sender's wire models, its view of a nightly run and its record of the nights it has settled.

The command and answer models mirror the fields of the collect effect's collect and publish operations. The sender does
not import that package: an answer is read through a view that keeps only the fields the sender decides on, and the collect
answer is forwarded to the publish command as it arrived.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCollectStatus(StrEnum):
    DISABLED = "disabled"
    NO_RUN = "no_run"
    WAITING = "waiting"
    READ_FAILED = "read_failed"
    COLLECTED = "collected"


class EnumPublishStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"
    NO_COLLECTOR_RESULT = "no_collector_result"
    NOT_COLLECTED = "not_collected"
    DISABLED = "disabled"
    NO_RUN = "no_run"
    READ_FAILED = "read_failed"
    WRITE_PENDING = "write_pending"


class EnumSlotOutcome(StrEnum):
    SENT = "sent"
    MISSED = "missed"


class ModelCollectCommand(_Frozen):
    """The collect operation's request: the slot date and the read time."""

    slot_date: date
    now: AwareDatetime


class ModelCollectAnswer(BaseModel):
    """The fields of the collect result the sender decides on; the full result is kept in ``raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumCollectStatus
    slot_date: date
    failure: str = ""
    raw: dict[str, JsonValue]


class ModelPublishCommand(_Frozen):
    """The publish operation's request; ``collect_result`` is the collect answer as it arrived."""

    slot_date: date
    now: AwareDatetime
    collect_result: dict[str, JsonValue]
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    stage_entry_counted: int = Field(ge=0, le=4)


class ModelPublishAnswer(_Frozen):
    status: EnumPublishStatus
    slot_date: date
    failure: str = ""


class ModelRecordSettings(_Frozen):
    """The values of the publish command that come from the selection policy, never from the sender's overlay."""

    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    stage_entry_counted: int = Field(ge=0, le=4)


class ModelNightlyRunView(_Frozen):
    """One run of the nightly workflow as the repository gateway lists it."""

    run_id: int = Field(ge=1)
    status: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def terminal(self) -> bool:
        return self.status == "completed"


class ModelSlotRecord(_Frozen):
    """One night the sender has settled: sent, or given up as a missed night."""

    slot_date: date
    outcome: EnumSlotOutcome
    recorded_at: AwareDatetime
    detail: str


class ModelSenderState(_Frozen):
    slots: tuple[ModelSlotRecord, ...] = ()


__all__: list[str] = [
    "EnumCollectStatus",
    "EnumPublishStatus",
    "EnumSlotOutcome",
    "ModelCollectAnswer",
    "ModelCollectCommand",
    "ModelNightlyRunView",
    "ModelPublishAnswer",
    "ModelPublishCommand",
    "ModelRecordSettings",
    "ModelSenderState",
    "ModelSlotRecord",
]
