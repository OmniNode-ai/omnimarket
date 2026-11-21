# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The public-safe record published for one nightly run, which the stage gate validates a receipt against."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelPublishedAttributionRecord(BaseModel):
    """One nightly run's attribution counts: public-safe fields only, no private names.

    The nightly collect effect publishes one per night. The stage gate reads them through
    ``ProtocolAttributionRecordSource``; the receipt's figures must equal what these say.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1)
    #: The date of the nightly's scheduled slot (not of the run that happened to execute it).
    slot_date: datetime.date
    nightly_run_id: int = Field(ge=1)
    #: The commit the nightly suite ran on.
    tested_head_sha: str = Field(min_length=1)
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    #: The stage whose ``selection_by_stage`` entry the counts below are for.
    stage_entry_counted: int = Field(ge=0, le=4)
    #: Merged pull requests whose record held that entry at these versions.
    pr_record_count: int = Field(ge=0)
    miss_count: int = Field(ge=0)
    interaction_count: int = Field(ge=0)
    preexisting_count: int = Field(ge=0)
    unattributable_count: int = Field(ge=0)
    incomplete_count: int = Field(ge=0)
