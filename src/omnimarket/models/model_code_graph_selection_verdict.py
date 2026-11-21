# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The written verdict of the selection comparison: two parts, neither of which may stay unknown.

Part one: did the graph find tests the parse did not, over at least ``graph_min_valid_comparisons``
valid comparisons (else ``insufficient``), judged once the comparison has run ``window_days`` days.
Part two: did a graph-only test later fail in a nightly, judged once the miss detector has run
``nightly_nights_required`` nights. Before its time a part is ``pending``; after its time ``pending``
is refused by the model, so a verdict can never stay unknown past its date.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_code_graph_selection_compare import (
    ModelSelectionCompareRow,
)


class EnumPartOneVerdict(StrEnum):
    PENDING = "pending"
    #: Fewer valid comparisons than the policy's minimum: never a pass.
    INSUFFICIENT = "insufficient"
    GRAPH_FOUND_TESTS_PARSE_MISSED = "graph_found_tests_parse_missed"
    GRAPH_FOUND_NOTHING_EXTRA = "graph_found_nothing_extra"


class EnumPartTwoVerdict(StrEnum):
    PENDING = "pending"
    #: No valid comparison named a graph-only test, so there is nothing to attribute.
    NOT_APPLICABLE = "not_applicable"
    GRAPH_ONLY_TEST_FAILED = "graph_only_test_failed"
    NO_GRAPH_ONLY_TEST_FAILED = "no_graph_only_test_failed"


class ModelLaterNightlyFailure(BaseModel):
    """A test a nightly attributed to a merged pull request (the miss detector's record)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    test_path: str = Field(min_length=1)


class ModelSelectionVerdictRequest(BaseModel):
    """Everything a verdict is a function of; it reads no clock, file or network."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelSelectionCompareRow, ...]
    #: When the comparison began and the instant the verdict is written at.
    window_start: datetime
    now: datetime
    #: Policy key, fixed before the window starts and not changed during it.
    graph_min_valid_comparisons: int = Field(ge=1)
    window_days: int = Field(default=7, ge=1)
    #: Nights the miss detector has completed; None where it has not run at all.
    nightly_nights_observed: int | None = Field(default=None, ge=0)
    nightly_nights_required: int = Field(default=7, ge=1)
    nightly_failures: tuple[ModelLaterNightlyFailure, ...] = ()

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Self:
        if self.now < self.window_start:
            raise ValueError("the verdict is written after the window starts")
        return self


class ModelSelectionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    part_one: EnumPartOneVerdict
    part_two: EnumPartTwoVerdict
    days_elapsed: int = Field(ge=0)
    window_days: int = Field(ge=1)
    nights_observed: int | None = Field(ge=0)
    nights_required: int = Field(ge=1)
    graph_min_valid_comparisons: int = Field(ge=1)
    valid_comparisons: int = Field(ge=0)
    graph_only_total: int = Field(ge=0)
    parse_only_total: int = Field(ge=0)
    both_total: int = Field(ge=0)
    excluded_snapshot_mismatch: int = Field(ge=0)
    excluded_file_store: int = Field(ge=0)
    excluded_historical: int = Field(ge=0)
    excluded_lookup_failed: int = Field(ge=0)
    #: Graph-only tests a later nightly failed: ``repo#pr:path``.
    graph_only_failures: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _no_unknown_past_its_date(self) -> Self:
        if (
            self.part_one is EnumPartOneVerdict.PENDING
            and self.days_elapsed >= self.window_days
        ):
            raise ValueError(
                f"part one is still pending after {self.days_elapsed} days: the window is {self.window_days}"
            )
        if self.part_two is EnumPartTwoVerdict.PENDING and (
            self.nights_observed is not None
            and self.nights_observed >= self.nights_required
        ):
            raise ValueError(
                f"part two is still pending after {self.nights_observed} nights: {self.nights_required} are required"
            )
        return self
