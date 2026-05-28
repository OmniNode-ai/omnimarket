# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_feature_dashboard_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CHECK_TYPES = (
    "skill_doc",
    "backing_node",
    "contract",
    "handler",
    "models",
    "tests",
    "entry_point",
    "runtime_topics",
)


class ModelFeatureDashboardRequest(BaseModel):
    """Request to audit skill connectivity across platform layers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skills: list[str] | None = Field(
        default=None,
        description="Optional skill name filter; null means all skills",
    )
    check_types: list[str] | None = Field(
        default=None,
        description="Layer check types to run; null means all 8 layers",
    )
    repo_root: str | None = Field(
        default=None,
        description="Repository root to audit; null means infer from this package path.",
    )

    @field_validator("skills", "check_types")
    @classmethod
    def _normalize_optional_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("entries must not be blank")
            if text not in seen:
                normalized.append(text)
                seen.add(text)
        return normalized

    @field_validator("check_types")
    @classmethod
    def _validate_check_types(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [item for item in value if item not in DEFAULT_CHECK_TYPES]
        if unknown:
            raise ValueError(
                "unknown check_types "
                + ", ".join(unknown)
                + "; expected one of "
                + ", ".join(DEFAULT_CHECK_TYPES)
            )
        return value


__all__ = ["DEFAULT_CHECK_TYPES", "ModelFeatureDashboardRequest"]
