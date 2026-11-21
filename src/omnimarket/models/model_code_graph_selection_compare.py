# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the selection comparison: the code graph's REACH answer beside the parse selection.

One row compares one merged pull request. A row counts the test files only the graph reached, only
the parse selected and both; a row that is not a clean comparison (a snapshot that is not the pull
request's base, a historical merge, a failed lookup) counts none of them. The comparison reads and
appends and gates nothing.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from omnimarket.models.model_code_graph_lookup import EnumLookupFallbackReason


class EnumCompareOutcome(StrEnum):
    """What one comparison came to."""

    #: The graph answered for the pull request's base and the counts are stated.
    COMPARED = "compared"
    #: The graph's indexed commit is not the pull request's base: left out of every count.
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    #: A merge from before the selection record existed: a smoke test of the deadline path only.
    HISTORICAL_UNVERSIONED = "historical_unversioned"
    #: Neither store answered inside its deadline, or the graph refused the statement.
    LOOKUP_FAILED = "lookup_failed"


class EnumGraphAnswerSource(StrEnum):
    MEMGRAPH = "memgraph"
    FILE_STORE = "file-store"
    NONE = "none"


class ModelLookupDeadlineExceeded(BaseModel):
    """A lookup the caller terminated at its deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["memgraph", "file-store"]
    deadline_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)


class ModelCodeGraphCompareSettings(BaseModel):
    """The overlay's ``compare`` block: the two deadlines of a comparison, in seconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The most the Memgraph lookup may take before the caller terminates its worker.
    lookup_deadline_seconds: float = Field(gt=0)
    #: The most the file-store fallback may take; separate so the lookup cannot consume it.
    fallback_deadline_seconds: float = Field(gt=0)


class ModelParseSelection(BaseModel):
    """The parse side: the selection entry of the stage fixed in the policy, taken from the record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: int = Field(ge=0)
    #: Directories (trailing slash) and test files; ``tests/`` is the whole suite.
    selected_paths: tuple[str, ...] = Field(min_length=1)
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)


class ModelSelectionCompareCommand(BaseModel):
    """One merged pull request to compare."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pr_number: int | None = Field(ge=1)
    head_sha: str = Field(min_length=1)
    base_sha: str = Field(min_length=1)
    changed_files: tuple[str, ...] = Field(min_length=1)
    parse: ModelParseSelection
    #: Every test file of the repository at the base: what a selected directory expands to.
    test_files: tuple[str, ...] = Field(min_length=1)
    #: A merge from before the record existed (the 60 historical merges).
    historical: bool = False
    #: Whether the row is appended through the configured appender; a command that does not say writes nothing.
    write: bool = False


class ModelSelectionCompareRow(BaseModel):
    """The comparison of one pull request. Every count is required; a non-comparison counts none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    pr_number: int | None = Field(ge=1)
    base_sha: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    compared_at: datetime
    outcome: EnumCompareOutcome
    compared_stage: int = Field(ge=0)
    selector_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    graph_source: EnumGraphAnswerSource
    #: The commit the graph answered for; None when no store answered.
    indexed_commit: str | None = None
    fallback_reason: EnumLookupFallbackReason | None = None
    graph_only: int = Field(ge=0)
    parse_only: int = Field(ge=0)
    both: int = Field(ge=0)
    #: The test files only the graph reached (kept so a later nightly failure can be attributed).
    graph_only_paths: tuple[str, ...] = ()
    deadlines: tuple[ModelLookupDeadlineExceeded, ...] = ()
    elapsed_seconds: float = Field(ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def _counts_belong_to_a_comparison(self) -> Self:
        if self.outcome is not EnumCompareOutcome.COMPARED and (
            self.graph_only or self.parse_only or self.both or self.graph_only_paths
        ):
            raise ValueError(
                f"a {self.outcome.value} row is in no count: its three counts are zero"
            )
        if len(self.graph_only_paths) != self.graph_only:
            raise ValueError("graph_only_paths must name exactly graph_only files")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def valid_for_verdict(self) -> bool:
        """A clean comparison against Memgraph; a file-store answer is excluded from the graph verdict."""
        return (
            self.outcome is EnumCompareOutcome.COMPARED
            and self.graph_source is EnumGraphAnswerSource.MEMGRAPH
        )


class ModelSelectionCompareResult(BaseModel):
    """The row, and whether the appender took it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelSelectionCompareRow
    ledger: Literal["written", "already-written", "not-written", "append-failed"]
    failure: str | None = None
