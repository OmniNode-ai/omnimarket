# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing scope overlay and the caller's result of reading it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class ModelPrHandoffLandingScopeOverlay(BaseModel):
    """The document at ``<root>/landing_scope/overlay.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...]

    @field_validator("repos")
    @classmethod
    def _usable_repos(cls, repos: tuple[str, ...]) -> tuple[str, ...]:
        if any(not repo.strip() or repo != repo.strip() for repo in repos):
            raise ValueError(
                "repos entries must be non-blank and have no surrounding whitespace"
            )
        return repos


class ModelPrHandoffLandingScopeReading(BaseModel):
    """The scope, or why it could not be read; both None means no scope load."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] | None = None
    problem: str | None = None


__all__: list[str] = [
    "ModelPrHandoffLandingScopeOverlay",
    "ModelPrHandoffLandingScopeReading",
]
