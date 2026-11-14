# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scan command and the scan policy (the contract's ``config.scan`` block, or an overlay that replaces it)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.effect_bypass import (
    EnumBypassCategory,
)


class ModelBypassScanCommand(BaseModel):
    """Which checkout to scan, and the repository name its baseline row is under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    root: str = Field(min_length=1)


class ModelBypassScanPolicy(BaseModel):
    """What is scanned and what counts. Nothing here has a default: an absent key refuses the load."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suffixes: tuple[str, ...] = Field(min_length=1)
    exclude_globs: tuple[str, ...]
    comment_prefixes: tuple[str, ...]
    categories: dict[EnumBypassCategory, tuple[str, ...]] = Field(min_length=1)
    #: Per category, the globs of the node that IS that outside world's effect node: a line there is not a bypass of that category.
    sanctioned: dict[EnumBypassCategory, tuple[str, ...]]

    @model_validator(mode="after")
    def _patterns_compile(self) -> ModelBypassScanPolicy:
        for category, patterns in self.categories.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"category {category.value}: pattern {pattern!r} does not compile: {exc}"
                    ) from exc
        return self
