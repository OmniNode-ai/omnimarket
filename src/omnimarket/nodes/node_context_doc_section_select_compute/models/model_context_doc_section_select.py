# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the doc-section select compute (OMN-21126).

The caller passes document text that a git source read at a pinned commit, the
tier and rule data that names which headings each tier may take and when, the
plan lifecycle rows, and the commit's tree paths. Tiers and triggers come only
from that data: an unknown key fails validation. The result carries one
context-source entry per admitted section and lists exclusions with their
reasons. No token budget and no pack hash belong here: the builder owns both.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, unique
from typing import Annotated, Self

from omnibase_core.enums.enum_context_factor import EnumContextFactor
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
    "SECTION_FACTORS",
    "EnumDocSectionExclusionReason",
    "EnumDocSectionSelectError",
    "ModelCitedPath",
    "ModelDocSectionDocument",
    "ModelDocSectionExclusion",
    "ModelDocSectionRule",
    "ModelDocSectionSelectRequest",
    "ModelDocSectionSelectResult",
    "ModelDocSectionTier",
    "ModelPlanLifecycleRow",
    "ModelSelectedDocSection",
]

_NonBlank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]

SECTION_FACTORS: frozenset[EnumContextFactor] = frozenset(
    {
        EnumContextFactor.CANONICAL_SHAPES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.PLAN_EXCERPTS,
    }
)


@unique
class EnumDocSectionExclusionReason(StrEnum):
    """Why a triggered section was left out of the result."""

    AMBIGUOUS_DEFINITION = "ambiguous_definition"
    SECTION_MISSING = "section_missing"
    DUPLICATE_HEADING = "duplicate_heading"
    PLAN_NOT_ACTIVE = "plan_not_active"
    PLAN_NOT_LISTED = "plan_not_listed"
    NOT_CONTIGUOUS = "not_contiguous"


@unique
class EnumDocSectionSelectError(StrEnum):
    """Typed refusals: more than one document at the same path."""

    DUPLICATE_PATH = "duplicate_path"


def _repository_relative(value: str) -> str:
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("path must be repository-relative")
    return value


class ModelDocSectionDocument(BaseModel):
    """One document's path and text as read at the pinned commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank = Field(description="Repository-relative document path")
    file_text: str = Field(description="Document text read at commit_sha")

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _repository_relative(value)


class ModelDocSectionTier(BaseModel):
    """One tier: an id and the context factor its entries carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_id: _NonBlank
    factor: EnumContextFactor

    @field_validator("factor")
    @classmethod
    def _section_factor(cls, value: EnumContextFactor) -> EnumContextFactor:
        if value not in SECTION_FACTORS:
            raise ValueError(f"{value.value} is not a document-section factor")
        return value


class ModelDocSectionRule(BaseModel):
    """One heading of one document that a tier may take, and when.

    A rule fires when ``always_inject`` is set or the request's task kind is one
    of ``task_kinds``. A rule with neither could never fire and is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_id: _NonBlank
    document_path: _NonBlank
    heading: _NonBlank
    always_inject: bool = False
    task_kinds: tuple[_NonBlank, ...] = ()

    @field_validator("document_path")
    @classmethod
    def _relative(cls, value: str) -> str:
        return _repository_relative(value)

    @model_validator(mode="after")
    def _can_fire(self) -> Self:
        if not self.always_inject and not self.task_kinds:
            raise ValueError("a rule needs always_inject or at least one task kind")
        return self


class ModelPlanLifecycleRow(BaseModel):
    """One plan's lifecycle row; only status ``active`` admits its excerpts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank
    status: _NonBlank
    resolved_status: str | None = None


class ModelDocSectionSelectRequest(BaseModel):
    """The documents at a pinned commit and the data that selects sections."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    as_of: datetime = Field(description="Commit time of commit_sha")
    task_kind: str | None = None
    tiers: tuple[ModelDocSectionTier, ...]
    rules: tuple[ModelDocSectionRule, ...]
    documents: tuple[ModelDocSectionDocument, ...]
    plan_lifecycle: tuple[ModelPlanLifecycleRow, ...] = ()
    tree_paths: tuple[str, ...] = Field(
        default=(), description="Every path of the tree at commit_sha"
    )

    @model_validator(mode="after")
    def _rules_name_known_tiers(self) -> Self:
        tier_ids = [tier.tier_id for tier in self.tiers]
        if len(tier_ids) != len(set(tier_ids)):
            raise ValueError("tier ids must be unique")
        unknown = sorted({rule.tier_id for rule in self.rules} - set(tier_ids))
        if unknown:
            raise ValueError(f"rules name unknown tiers: {unknown}")
        return self


class ModelCitedPath(BaseModel):
    """A repository path an excerpt cites, and whether the tree holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: _NonBlank
    grounded: bool


class ModelSelectedDocSection(BaseModel):
    """An admitted section: its tier, source and context-source entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_id: _NonBlank
    path: _NonBlank
    heading: _NonBlank
    entry: ModelContextSourceEntry
    cited_paths: tuple[ModelCitedPath, ...] = ()
    resolved_status: str | None = None


class ModelDocSectionExclusion(BaseModel):
    """A triggered section left out of the result, with the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_id: _NonBlank
    path: _NonBlank
    heading: _NonBlank
    reason: EnumDocSectionExclusionReason
    detail: str | None = None
    source_ref: _NonBlank


class ModelDocSectionSelectResult(BaseModel):
    """Selected sections in tier, path and line order, and the exclusions.

    ``RESOLVED`` carries at least one selection; ``AMBIGUOUS`` means nothing
    was admitted and at least one definition conflicted; ``UNRESOLVED`` means
    nothing was admitted; ``UNAVAILABLE`` carries a typed error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextSourceStatus
    selected: tuple[ModelSelectedDocSection, ...] = ()
    excluded: tuple[ModelDocSectionExclusion, ...] = ()
    error_code: EnumDocSectionSelectError | None = None
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
        """The context-source entries, in result order."""
        return tuple(item.entry for item in self.selected)
