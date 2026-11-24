# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the no-file census (OMN-22917, piece 6): overlay, request and result.

Every setting is in the overlay; no field has a default that names a file, a variable or a path.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCensusRepository(_Frozen):
    """A registered repository, a directory directly under the registry root."""

    name: str = Field(min_length=1)
    own_line: bool = Field(
        description="Reported on its own line, not in the total (decision 3: an owner outside this plan)."
    )


class ModelCensusOverlay(_Frozen):
    registry_root_env: str = Field(min_length=1)
    repositories: tuple[ModelCensusRepository, ...] = Field(min_length=1)
    names: tuple[str, ...] = Field(
        min_length=1, description="The ledger file's name, with its old names."
    )
    path_variables: tuple[str, ...] = Field(
        min_length=1, description="Variables that carry the ledger file's path."
    )
    citation_suffix: str = Field(
        min_length=1,
        description="Regex that, directly after a name, marks a line citation (a ruling's file:line).",
    )
    document_suffixes: tuple[str, ...] = Field(
        description="Files that are documents: every mention is a citation, none is a reader or writer."
    )
    code_suffixes: tuple[str, ...]
    code_names: tuple[str, ...] = Field(
        description="Extensionless files read as code, by name."
    )
    skip_dirs: tuple[str, ...]
    max_file_bytes: int = Field(gt=0)
    hash_comment_suffixes: tuple[str, ...]
    slash_comment_suffixes: tuple[str, ...]
    writer_markers: tuple[str, ...] = Field(
        description="Regexes that, on a line naming the ledger, mark a write (the rest are reads)."
    )
    journal_env: str = Field(
        min_length=1,
        description="Variable naming the emission journal file; unset means the in-memory sink.",
    )
    ratchet_file: str = Field(
        min_length=1,
        description="Path of the shrink-only ratchet file, relative to the working directory.",
    )
    daily_history_file: str = Field(
        min_length=1,
        description="File name, in the daily unit's state directory, of the dated daily records the day-over-day change is "
        "read from.",
    )
    daily_history_days: int = Field(
        gt=0,
        description="How many days of daily records are kept in that file.",
    )
    runtime_include: tuple[str, ...] = Field(
        description="Regexes (searched in the path) that mark runtime code: the source tree and the runtime contracts "
        "that sit in it. A path no pattern marks is not runtime code.",
    )
    runtime_exclude: tuple[str, ...] = Field(
        description="Regexes (searched in the path) that take a path out of runtime code again: tests, fixtures, docs, "
        "data and plan contracts, whatever tree they sit in.",
    )
    per_file: bool = Field(
        default=False,
        description="Hold the per-file rows (the ``files`` table of the ratchet file) as well as the count. "
        "A file with no row that holds an executable reader or writer, or a file that holds more of "
        "them than its row, fails the run (new code opening the ledger file).",
    )

    @field_validator("names", "path_variables")
    @classmethod
    def _nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("a blank name matches every line")
        return value


class EnumCensusKind(StrEnum):
    READER = "reader"
    WRITER = "writer"


class ModelCensusHit(_Frozen):
    path: str
    line: int = Field(ge=1)
    kind: EnumCensusKind


class ModelRepositoryCount(_Frozen):
    repository: str
    own_line: bool
    present: bool
    files: int = Field(ge=0)
    runtime_files: int = Field(
        default=0,
        ge=0,
        description="Of ``files``, those that are runtime code (the overlay's runtime patterns), counted apart from "
        "tests, docs, data and plan contracts.",
    )
    readers: int = Field(ge=0)
    writers: int = Field(ge=0)
    hits: tuple[ModelCensusHit, ...] = ()
    ratchet: int | None = None
    state: str = Field(
        description="ok, lowerable, grew, new-reader, absent, unreadable, unratcheted or unbaselined"
    )
    new_readers: tuple[str, ...] = Field(
        default=(),
        description="Files the per-file baseline does not allow: 'path (hits>allowed)', allowed 0 for a new file.",
    )


class ModelCensusRequest(_Frozen):
    correlation_id: str = Field(min_length=1)
    repositories: tuple[str, ...] | None = Field(
        default=None,
        description="Restrict to these registered names; None is every one.",
    )


class ModelCensusResult(_Frozen):
    correlation_id: str
    passed: bool
    total_files: int = Field(
        ge=0, description="Files in repositories that are not on their own line."
    )
    runtime_files: int = Field(
        default=0,
        ge=0,
        description="Runtime-code files in repositories that are not on their own line.",
    )
    counts: tuple[ModelRepositoryCount, ...]
    failure_code: str | None = None
    failure_detail: str = ""
    absent_repositories: tuple[str, ...] = Field(
        default=(),
        description="Registered repositories with no clone, in registry order. They are not counted and do not fail the "
        "run by themselves; the run counts every present repository and names each absent one.",
    )
    lowered_ratchet: dict[str, int] = Field(
        default_factory=dict,
        description="The ratchet with every lowerable count lowered; never one raised.",
    )
    lowered_baseline: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description="The per-file baseline with every file's row lowered to its count and every cleared "
        "file dropped; never a row raised and never a file added.",
    )


class ModelCensusDailyRow(_Frozen):
    repository: str
    own_line: bool
    files: int = Field(ge=0)
    runtime_files: int = Field(ge=0)
    ratchet: int | None = None
    state: str


class ModelCensusDailyRecord(_Frozen):
    """The daily record: the per-repository counts and the runtime-code count as one typed value."""

    correlation_id: str
    recorded_on: str = Field(description="UTC date, YYYY-MM-DD.")
    passed: bool
    failure_code: str | None = None
    total_files: int = Field(ge=0)
    runtime_files: int = Field(ge=0)
    repositories: tuple[ModelCensusDailyRow, ...]
    absent_repositories: tuple[str, ...] = Field(
        default=(),
        description="Registered repositories with no clone, which the counts leave out.",
    )

    @classmethod
    def from_result(
        cls, result: ModelCensusResult, recorded_on: str
    ) -> ModelCensusDailyRecord:
        return cls(
            correlation_id=result.correlation_id,
            recorded_on=recorded_on,
            passed=result.passed,
            failure_code=result.failure_code,
            total_files=result.total_files,
            runtime_files=result.runtime_files,
            absent_repositories=result.absent_repositories,
            repositories=tuple(
                ModelCensusDailyRow(
                    repository=c.repository,
                    own_line=c.own_line,
                    files=c.files,
                    runtime_files=c.runtime_files,
                    ratchet=c.ratchet,
                    state=c.state,
                )
                for c in result.counts
            ),
        )
