# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Frozen catalog entries and request/result interfaces for review consumers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.enums.enum_review_defect_catalog import (
    EnumReviewDefectCatalogOutcome,
    EnumReviewDefectCatalogRefusal,
    EnumReviewDefectCheckability,
    EnumReviewDefectFamily,
    EnumReviewDefectSeverity,
    EnumReviewDefectStatus,
)

_SEMVER_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# The plan's 5,000-token ceiling for the rendered catalog section; a request may only tighten it.
MAX_REVIEW_DEFECT_TOKEN_BUDGET = 5000


class ModelReviewDefectEnforcement(BaseModel):
    """A repository's validator that enforces a defect code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    validator_id: str = Field(..., min_length=1)
    validator_version: str | None = None


class ModelReviewDefectCode(BaseModel):
    """One stable defect identifier, including its detection and repair guidance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., pattern=r"^[ACFKPSTY][0-9]{2}$")
    slug: str = Field(..., pattern=r"^[a-z][a-z0-9-]*$")
    family: EnumReviewDefectFamily
    default_severity: EnumReviewDefectSeverity
    title: str
    detect: str
    fix_hint: str
    checkability: EnumReviewDefectCheckability
    status: EnumReviewDefectStatus
    enforced_by: dict[str, ModelReviewDefectEnforcement] = Field(default_factory=dict)
    introduced_in: str
    retired_in: str | None = None
    examples: tuple[str, ...] = ()

    @field_validator("title")
    @classmethod
    def _title_word_limit(cls, value: str) -> str:
        """Keep the title within twelve whitespace-separated words."""
        if len(value.split()) > 12:
            raise ValueError("title must contain at most 12 words")
        return value

    @field_validator("detect")
    @classmethod
    def _detect_word_limit(cls, value: str) -> str:
        """Keep detection guidance within forty whitespace-separated words."""
        if len(value.split()) > 40:
            raise ValueError("detect must contain at most 40 words")
        return value

    @field_validator("fix_hint")
    @classmethod
    def _fix_hint_word_limit(cls, value: str) -> str:
        """Keep repair guidance within thirty whitespace-separated words."""
        if len(value.split()) > 30:
            raise ValueError("fix_hint must contain at most 30 words")
        return value


class ModelReviewDefectCatalog(BaseModel):
    """Versioned catalog in source order, including retired identifiers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_version: str = Field(..., pattern=_SEMVER_PATTERN)
    parent_version: str | None = None
    codes: tuple[ModelReviewDefectCode, ...]
    do_not_report: tuple[str, ...] = ()


class ModelReviewDefectCatalogRequest(BaseModel):
    """Catalog input and repository evidence used to render a review section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog_text: str | None = None
    repository: str = Field(..., min_length=1)
    requested_version: str | None = None
    token_budget: int | None = Field(
        default=None, ge=1, le=MAX_REVIEW_DEFECT_TOKEN_BUDGET
    )
    enforced_codes: tuple[str, ...] = ()


class ModelReviewDefectCatalogResult(BaseModel):
    """The rendered catalog and allowed codes, or a refusal with its location."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumReviewDefectCatalogOutcome
    catalog_version: str | None
    content_hash: str | None
    rendered_section: str | None
    rendered_tokens: int | None
    active_codes: tuple[str, ...]
    code_families: dict[str, str]
    suppressed_codes: tuple[str, ...]
    refusal_reason: EnumReviewDefectCatalogRefusal | None
    offending_entry: str | None
    detail: str


__all__ = [
    "MAX_REVIEW_DEFECT_TOKEN_BUDGET",
    "ModelReviewDefectCatalog",
    "ModelReviewDefectCatalogRequest",
    "ModelReviewDefectCatalogResult",
    "ModelReviewDefectCode",
    "ModelReviewDefectEnforcement",
]
