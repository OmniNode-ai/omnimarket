# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed baseline receipt of the selection replay, and the observed section it keeps apart.

The receipt has two sections that never mix. ``counterfactual`` is what the selector decides over the
frozen tree for each merge; it is derived from the replay input and re-derives byte for byte. ``observed`` is
what CI actually did, read from run records; it is frozen input, kept in its own file, and never re-derived.
Every observed value names the run ids it came from, or is the explicit state ``unavailable``; a timing
statistic states its sample size.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    EnumReplayKind,
    ModelReplayResult,
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"


class EnumObservedState(StrEnum):
    READ = "read"
    UNAVAILABLE = "unavailable"


class ModelObservedValue(BaseModel):
    """One observed value: read from named runs, or explicitly unavailable (never an omitted key or a zero)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumObservedState
    value: float | str | None
    unit: str | None = None
    source_run_ids: tuple[int, ...] = ()
    sample_size: int = Field(ge=0)
    reason: str | None = None
    """Why the value is unavailable (required then, forbidden otherwise)."""

    @model_validator(mode="after")
    def _read_or_unavailable(self) -> ModelObservedValue:
        if self.state is EnumObservedState.READ:
            if self.value is None:
                raise ValueError("a read observed value needs its value")
            if not self.source_run_ids:
                raise ValueError("a read observed value names its source run ids")
            if self.sample_size < 1:
                raise ValueError("a read observed value states its sample size")
            if self.reason is not None:
                raise ValueError("a read observed value has no unavailable reason")
        else:
            if self.value is not None or self.source_run_ids or self.sample_size != 0:
                raise ValueError(
                    "an unavailable observed value has no value, run ids or sample"
                )
            if not self.reason:
                raise ValueError("an unavailable observed value states why")
        return self


class ModelObservedMerge(BaseModel):
    """The CI run record found for one merge's pull request head."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(pattern=_HEX40)
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=_HEX40)
    state: EnumObservedState
    run_id: int | None = None
    conclusion: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _read_or_unavailable(self) -> ModelObservedMerge:
        if self.state is EnumObservedState.READ:
            if None in (self.run_id, self.conclusion, self.created_at, self.updated_at):
                raise ValueError(
                    "a read merge record names its run, conclusion and times"
                )
        elif self.run_id is not None or not self.reason:
            raise ValueError("an unavailable merge record has no run and states why")
        return self


class ModelObservedSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enable_smart_tests: ModelObservedValue
    runner_minutes_median: ModelObservedValue
    runner_minutes_p90: ModelObservedValue
    shard_setup_seconds: ModelObservedValue
    flag_partial_reading: str | None = None
    """What was read of the flag when its effective value could not be established."""
    merges: tuple[ModelObservedMerge, ...]


class ModelDurationsReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: int = Field(ge=0)
    total_seconds: float = Field(ge=0)
    file_sha256: str = Field(pattern=_HEX64)
    test_files: int = Field(ge=0)
    """Files under ``tests/`` named ``test_*.py`` or ``*_test.py`` at the pinned tree."""
    extra_entries: tuple[str, ...]
    """Entries of the durations file that name no test file under ``tests/`` at the pinned tree."""
    extra_entries_seconds: float = Field(ge=0)
    extra_entries_counted_in_total: bool

    @model_validator(mode="after")
    def _extras_are_the_difference(self) -> ModelDurationsReading:
        if not self.extra_entries_counted_in_total:
            raise ValueError("the selector sums every entry, so the extras count")
        return self


class ModelPremiseCheck(BaseModel):
    """One figure of the plan, beside what the replay measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    premise: str
    plan_value: str
    measured_value: str
    holds: bool

    @model_validator(mode="after")
    def _holds_is_equality(self) -> ModelPremiseCheck:
        if self.holds != (self.plan_value == self.measured_value):
            raise ValueError("holds must be plan_value == measured_value")
        return self


class ModelBaselineReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1)
    pinned_head: str = Field(pattern=_HEX40)
    tree_id: str = Field(pattern=_HEX40)
    replay_input_path: str
    replay_input_sha256: str = Field(pattern=_HEX64)
    observed_input_path: str
    observed_input_sha256: str = Field(pattern=_HEX64)
    durations: ModelDurationsReading
    premise_checks: tuple[ModelPremiseCheck, ...]
    observed: ModelObservedSection
    counterfactual: ModelReplayResult
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _totals_are_the_sums_of_the_rows(self) -> ModelBaselineReceipt:
        cf = self.counterfactual
        rows = cf.decisions
        totals = cf.totals
        if cf.tree_id != self.tree_id:
            raise ValueError("counterfactual tree id differs from the receipt's")
        if cf.stage != 0:
            raise ValueError("the baseline is the stage 0 replay")
        if totals.merges != len(rows):
            raise ValueError("totals.merges is not the row count")
        if len({row.sha for row in rows}) != len(rows):
            raise ValueError("a merge appears twice")
        if totals.narrowed != sum(r.kind is EnumReplayKind.NARROWED for r in rows):
            raise ValueError("totals.narrowed is not the sum of the rows")
        if totals.full_suite != sum(r.kind is EnumReplayKind.FULL_SUITE for r in rows):
            raise ValueError("totals.full_suite is not the sum of the rows")
        if totals.narrowed + totals.full_suite != totals.merges:
            raise ValueError("narrowed plus full-suite is not the merge count")
        for row in rows:
            if row.kind is EnumReplayKind.FULL_SUITE and not (
                row.primary_reason and row.triggers
            ):
                raise ValueError(f"full-suite row {row.sha} has no reason or trigger")
            if row.kind is EnumReplayKind.NARROWED and (
                row.primary_reason or row.triggers
            ):
                raise ValueError(f"narrowed row {row.sha} carries a full-suite reason")
        reasons = Counter(r.primary_reason for r in rows if r.primary_reason)
        if dict(sorted(reasons.items())) != totals.reason_counts:
            raise ValueError("totals.reason_counts is not the sum of the rows")
        if round(sum(r.selected_seconds for r in rows), 6) != totals.selected_seconds:
            raise ValueError("totals.selected_seconds is not the sum of the rows")
        if sum(r.split_count for r in rows) != totals.selected_shards:
            raise ValueError("totals.selected_shards is not the sum of the rows")
        tab = cf.infrastructure_cross_tab
        infra = reasons.get("test_infrastructure", 0)
        if tab.merges != infra or len(tab.rows) != infra:
            raise ValueError(
                "the cross-tab does not hold the test_infrastructure merges"
            )
        if sum(tab.cells.values()) != infra:
            raise ValueError("the cross-tab cells do not sum to the merge count")
        if {r.sha for r in tab.rows} != {
            r.sha for r in rows if r.primary_reason == "test_infrastructure"
        }:
            raise ValueError("the cross-tab names other merges than the rows do")
        if dict(sorted(Counter(r.cell for r in tab.rows).items())) != tab.cells:
            raise ValueError("the cross-tab cells are not the counts of its rows")
        if {m.sha for m in self.observed.merges} != {r.sha for r in rows}:
            raise ValueError("the observed section names other merges than the rows")
        if self.durations.total_seconds != cf.suite_seconds:
            raise ValueError("the durations total is not the replay's suite seconds")
        return self
