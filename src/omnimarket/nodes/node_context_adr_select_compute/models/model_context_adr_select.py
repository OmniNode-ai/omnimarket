# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the ADR select compute (OMN-21126).

The caller passes document file text that a git source read at a pinned commit,
the paths they were read from, that commit and the profile's ADR topics. The
result carries one context-source entry per admitted ADR and lists exclusions
with their reasons. No token budget and no pack hash belong here: the builder
owns both.
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from enum import StrEnum, unique
from typing import Annotated, Literal, Self

from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

__all__ = [
    "EnumAdrExclusionReason",
    "EnumAdrSelectError",
    "ModelAdrDocument",
    "ModelAdrExclusion",
    "ModelAdrFrontmatter",
    "ModelAdrSelectRequest",
    "ModelAdrSelectResult",
    "ModelSelectedAdr",
]

_NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


@unique
class EnumAdrExclusionReason(StrEnum):
    """Why an ADR was left out of the result."""

    NOT_ACCEPTED = "not_accepted"
    SUPERSEDED = "superseded"
    INVALID_FRONTMATTER = "invalid_frontmatter"
    EMPTY_BODY = "empty_body"


@unique
class EnumAdrSelectError(StrEnum):
    """Typed refusals: more than one document at the same path."""

    DUPLICATE_PATH = "duplicate_path"


class ModelAdrDocument(BaseModel):
    """One ADR file's path and text as read at the pinned commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank = Field(description="Repository-relative ADR path")
    file_text: str = Field(description="ADR file text read at commit_sha")

    @field_validator("path")
    @classmethod
    def _repository_relative(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("path must be repository-relative")
        return value


class ModelAdrSelectRequest(BaseModel):
    """The ADR documents at a pinned commit and the topics to select by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    as_of: datetime = Field(description="Commit time of commit_sha")
    topics: tuple[_NonBlank, ...] = Field(min_length=1)
    documents: tuple[ModelAdrDocument, ...]


class ModelAdrFrontmatter(BaseModel):
    """One ADR header as the file declares it; an unknown key fails validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["adr"]
    status: _NonBlank
    date: str | date_type
    title: _NonBlank
    adr_id: str = Field(pattern=r"^ADR-\d{4}$")
    topics: tuple[str, ...]
    refs: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    superseded_by: tuple[str, ...] = ()
    summary: str | None = None


class ModelSelectedAdr(BaseModel):
    """An admitted ADR: its parsed fields and its context-source entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank
    adr_id: str = Field(pattern=r"^ADR-\d{4}$")
    title: _NonBlank
    topics: tuple[str, ...]
    entry: ModelContextSourceEntry


class ModelAdrExclusion(BaseModel):
    """An ADR left out of the result, with the reason and available header fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank
    reason: EnumAdrExclusionReason
    adr_id: str | None = None
    status: str | None = None
    superseded_by: tuple[str, ...] = ()
    detail: str | None = None
    source_ref: _NonBlank


class ModelAdrSelectResult(BaseModel):
    """Selected ADRs in path order, and the exclusions.

    ``RESOLVED`` carries at least one selection; ``UNRESOLVED`` means no ADR
    qualifies for the requested topics; ``UNAVAILABLE`` carries a typed error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextSourceStatus
    selected: tuple[ModelSelectedAdr, ...] = ()
    excluded: tuple[ModelAdrExclusion, ...] = ()
    error_code: EnumAdrSelectError | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _status_agrees(self) -> Self:
        if self.status is EnumContextSourceStatus.RESOLVED and not self.selected:
            raise ValueError("a RESOLVED result must carry at least one selection")
        if self.status is not EnumContextSourceStatus.RESOLVED and self.selected:
            raise ValueError(f"a {self.status.value} result carries no selection")
        if (self.error_code is None) != (
            self.status is not EnumContextSourceStatus.UNAVAILABLE
        ):
            raise ValueError(
                "error_code is required on UNAVAILABLE and refused otherwise"
            )
        return self

    @property
    def entries(self) -> tuple[ModelContextSourceEntry, ...]:
        """The context-source entries, in path order."""
        return tuple(item.entry for item in self.selected)
