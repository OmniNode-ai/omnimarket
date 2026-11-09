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
    readers: int = Field(ge=0)
    writers: int = Field(ge=0)
    hits: tuple[ModelCensusHit, ...] = ()
    ratchet: int | None = None
    state: str = Field(
        description="ok, lowerable, grew, absent, unreadable or unratcheted"
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
    counts: tuple[ModelRepositoryCount, ...]
    failure_code: str | None = None
    failure_detail: str = ""
    lowered_ratchet: dict[str, int] = Field(
        default_factory=dict,
        description="The ratchet with every lowerable count lowered; never one raised.",
    )
