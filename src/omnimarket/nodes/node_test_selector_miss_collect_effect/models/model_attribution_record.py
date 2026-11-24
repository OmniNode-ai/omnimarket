# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public-safe attribution record of one nightly, and the request, result and errors of its publication.

The record is the one thing the collector publishes about a night. Every field is a count, a date, a version, a run id or
a commit id, so nothing in it can carry free text. It is bound to the nightly run it describes by ``nightly_run_id`` and
``tested_head_sha``, and keyed by ``(repository, nightly_slot_date)``: one record per night, never per run.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelCollectResult,
)

#: One fixed-name ``name`` of the check run that carries a record is ``<prefix>:<slot date>``.
CHECK_RUN_NAME_SEPARATOR = ":"
#: The conclusion of the check run that carries each status: an incomplete night is never ``success``.
CONCLUSION_COMPLETE = "success"
CONCLUSION_INCOMPLETE = "neutral"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PublicSafetyError(ValueError):
    """The payload would name a private repository, host or person. Names the field and the reason, never the value."""

    code = "public_safety_refused"

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{self.code}: field {field} refused ({reason})")
        self.field = field
        self.reason = reason


class RecordBindingError(ValueError):
    """The record cannot be bound to the nightly run it would describe, so nothing is published."""

    code = "record_binding_refused"


class EnumRecordStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class EnumPublishStatus(StrEnum):
    #: This invocation delivered the record through the gateway.
    PUBLISHED = "published"
    #: The night's record was delivered by an earlier invocation; nothing was written.
    ALREADY_PUBLISHED = "already_published"
    #: No collector result is recorded for the night: nothing is published, the slot check alarms after the lag.
    NO_COLLECTOR_RESULT = "no_collector_result"
    #: The request's collect result is not a collected night (it carries no attribution).
    NOT_COLLECTED = "not_collected"
    DISABLED = "disabled"
    NO_RUN = "no_run"
    READ_FAILED = "read_failed"
    #: The record is stored and its delivery failed; the next invocation retries it.
    WRITE_PENDING = "write_pending"


class ModelPublishedAttributionRecord(_Frozen):
    """The public-safe record of one night. Fields are exactly these; any other field is refused."""

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    nightly_slot_date: date
    nightly_run_id: int = Field(ge=1)
    tested_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    selector_version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")
    policy_version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")
    #: The selection stage whose ``selection_by_stage`` entry the pull request record count counts.
    stage_entry_counted: int = Field(ge=0, le=4)
    #: Merged pull requests in the night's range whose record holds an entry for ``stage_entry_counted``.
    pull_request_record_count: int = Field(ge=0)
    miss_count: int = Field(ge=0)
    interaction_count: int = Field(ge=0)
    preexisting_count: int = Field(ge=0)
    unattributable_count: int = Field(ge=0)
    #: The reasons that made the night incomplete (0 for a complete night).
    incomplete_count: int = Field(ge=0)
    status: EnumRecordStatus

    @model_validator(mode="after")
    def _status_agrees_with_the_counts(self) -> Self:
        incomplete = self.status is EnumRecordStatus.INCOMPLETE
        if incomplete != (self.incomplete_count > 0):
            raise ValueError(
                "status incomplete if and only if incomplete_count is above zero"
            )
        if incomplete and (
            self.miss_count
            or self.interaction_count
            or self.preexisting_count
            or self.unattributable_count
        ):
            raise ValueError("an incomplete night attributes no file")
        return self

    @property
    def key(self) -> str:
        return f"{self.repository}|{self.nightly_slot_date.isoformat()}"

    @property
    def clean(self) -> bool:
        """True only for a complete night with no miss, no unattributable file and no incompleteness."""
        return (
            self.status is EnumRecordStatus.COMPLETE
            and self.miss_count == 0
            and self.unattributable_count == 0
            and self.incomplete_count == 0
        )


class ModelRecordCheckRun(_Frozen):
    """What the gateway's check-run write carries: the record, on the tested head commit, under a keyed name."""

    name: str = Field(min_length=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    conclusion: str
    title: str = Field(min_length=1)
    #: The record as canonical JSON.
    summary: str = Field(min_length=2)


class ModelPublishRecordRequest(_Frozen):
    slot_date: date
    now: AwareDatetime
    #: The collector's result for the night; the record's counts come from its attribution.
    collect_result: ModelCollectResult
    selector_version: str
    policy_version: str
    stage_entry_counted: int = Field(ge=0, le=4)


class ModelPublishRecordResult(_Frozen):
    status: EnumPublishStatus
    slot_date: date
    record: ModelPublishedAttributionRecord | None = None
    check_run_name: str = ""
    failure: str = ""


def check_run_name(prefix: str, slot_date: date) -> str:
    return f"{prefix}{CHECK_RUN_NAME_SEPARATOR}{slot_date.isoformat()}"


def check_run_for(
    record: ModelPublishedAttributionRecord, prefix: str
) -> ModelRecordCheckRun:
    complete = record.status is EnumRecordStatus.COMPLETE
    return ModelRecordCheckRun(
        name=check_run_name(prefix, record.nightly_slot_date),
        head_sha=record.tested_head_sha,
        conclusion=CONCLUSION_COMPLETE if complete else CONCLUSION_INCOMPLETE,
        title=(
            f"{record.repository} nightly {record.nightly_slot_date.isoformat()} "
            f"run {record.nightly_run_id}: {record.status.value}"
        ),
        summary=record.model_dump_json(),
    )


__all__: list[str] = [
    "CHECK_RUN_NAME_SEPARATOR",
    "CONCLUSION_COMPLETE",
    "CONCLUSION_INCOMPLETE",
    "EnumPublishStatus",
    "EnumRecordStatus",
    "ModelPublishRecordRequest",
    "ModelPublishRecordResult",
    "ModelPublishedAttributionRecord",
    "ModelRecordCheckRun",
    "PublicSafetyError",
    "RecordBindingError",
    "check_run_for",
    "check_run_name",
]
