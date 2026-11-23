# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a record source read for the nights of a receipt: the records it trusts and the nights it refuses."""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelReceiptNight,
)


class EnumRecordDefectKind(StrEnum):
    #: The published summary is absent or does not parse as a published attribution record.
    UNPARSEABLE = "unparseable"
    #: More than one record is published for the night.
    DUPLICATE = "duplicate"
    #: The record names another commit than the one it was published on.
    HEAD_MISMATCH = "head_mismatch"
    #: The record names another repository, slot date or nightly run than the night asked for.
    IDENTITY_MISMATCH = "identity_mismatch"


class ModelRecordDefect(BaseModel):
    """A night whose published record is refused. The detail names no content of the summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot_date: datetime.date
    nightly_run_id: int = Field(ge=1)
    kind: EnumRecordDefectKind
    detail: str = Field(min_length=1)


class ModelAttributionRecordReading(BaseModel):
    """Records that passed every check, and the nights whose record did not. A night in neither has no record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ModelPublishedAttributionRecord, ...] = ()
    defects: tuple[ModelRecordDefect, ...] = ()


class ModelAttributionRecordReadRequest(BaseModel):
    """The nights of one receipt, whose published records are wanted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    nights: tuple[ModelReceiptNight, ...]
