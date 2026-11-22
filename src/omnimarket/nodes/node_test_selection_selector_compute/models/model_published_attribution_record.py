# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public-safe record published for one nightly run, which the stage gate validates a receipt against."""

from __future__ import annotations

import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumRecordStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class ModelPublishedAttributionRecord(BaseModel):
    """One nightly run's attribution counts: public-safe fields only, no private names.

    The nightly collect effect publishes one per night, with the field names and constraints of its own record
    model. The stage gate reads them through ``ProtocolAttributionRecordSource``; the receipt's figures must
    equal what these say.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    #: The date of the nightly's scheduled slot (not of the run that happened to execute it).
    nightly_slot_date: datetime.date
    nightly_run_id: int = Field(ge=1)
    #: The commit the nightly suite ran on.
    tested_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    selector_version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")
    policy_version: str = Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")
    #: The stage whose ``selection_by_stage`` entry the counts below are for.
    stage_entry_counted: int = Field(ge=0, le=4)
    #: Merged pull requests whose record held that entry at these versions.
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
