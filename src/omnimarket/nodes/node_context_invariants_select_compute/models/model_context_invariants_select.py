# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the invariants select compute (OMN-21126).

The caller passes the registry file text that a git source read at a pinned
commit, the path it was read from, that commit and the profile's scopes. The
result carries one context-source entry per admitted invariant and lists every
superseded entry as excluded. No token budget and no pack hash belong here: the
builder owns both.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, unique
from typing import Annotated, Self

from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

__all__ = [
    "EnumInvariantExclusionReason",
    "EnumInvariantsSelectError",
    "ModelInvariantExclusion",
    "ModelInvariantRecord",
    "ModelInvariantsSelectRequest",
    "ModelInvariantsSelectResult",
    "ModelSelectedInvariant",
]

_NonBlank = Annotated[str, StringConstraints(min_length=1)]


@unique
class EnumInvariantExclusionReason(StrEnum):
    """Why an invariant in scope was left out of the result."""

    SUPERSEDED = "superseded"


@unique
class EnumInvariantsSelectError(StrEnum):
    """Typed refusals: a malformed registry, or a scope it does not declare."""

    INVALID_REGISTRY = "invalid_registry"
    UNKNOWN_SCOPE = "unknown_scope"


class ModelInvariantRecord(BaseModel):
    """One registry entry as the file declares it; an unknown key fails validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: _NonBlank
    statement: _NonBlank
    scope: tuple[_NonBlank, ...] = Field(min_length=1)
    ruled: dict[str, str]
    enforcement: _NonBlank
    status: str = Field(pattern=r"^(active|superseded)$")
    superseded_by: _NonBlank | None = None

    @model_validator(mode="after")
    def _superseded_names_its_successor(self) -> Self:
        if (self.status == "superseded") != (self.superseded_by is not None):
            raise ValueError(
                f"{self.id}: superseded_by is required on a superseded entry "
                "and refused on an active one"
            )
        return self


class ModelInvariantsSelectRequest(BaseModel):
    """The registry bytes at a pinned commit and the scopes to select by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: _NonBlank = Field(description="Repository-relative registry path")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    file_text: str = Field(description="Registry file text read at commit_sha")
    scopes: tuple[_NonBlank, ...] = Field(min_length=1)
    as_of: datetime = Field(description="Commit time of commit_sha")


class ModelSelectedInvariant(BaseModel):
    """An admitted invariant: its parsed fields and its context-source entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invariant_id: _NonBlank
    statement: str = Field(description="The statement exactly as the registry parses")
    enforcement: str = Field(description="The enforcement line exactly as parsed")
    scopes: tuple[str, ...]
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    entry: ModelContextSourceEntry


class ModelInvariantExclusion(BaseModel):
    """An invariant left out of the result, with the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invariant_id: _NonBlank
    reason: EnumInvariantExclusionReason
    superseded_by: str | None = None
    source_ref: _NonBlank


class ModelInvariantsSelectResult(BaseModel):
    """Selected invariants in file order, and the exclusions.

    ``RESOLVED`` carries at least one selection; ``UNRESOLVED`` means no active
    entry carries a requested scope; ``UNAVAILABLE`` carries a typed error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextSourceStatus
    selected: tuple[ModelSelectedInvariant, ...] = ()
    excluded: tuple[ModelInvariantExclusion, ...] = ()
    error_code: EnumInvariantsSelectError | None = None
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
        """The context-source entries, in file order."""
        return tuple(item.entry for item in self.selected)
