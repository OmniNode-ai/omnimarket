# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr-handoff install-proof decision and its deployment overlay (OMN-23616)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelInstallDeclaration(BaseModel):
    """The deployment overlay that binds the install-proof decision.

    Every field is required and supplied by the overlay; no default is declared
    here, so an overlay that omits one fails to build rather than silently
    loosening the decision.
    """

    model_config = _FROZEN

    paths: tuple[str, ...] = Field(min_length=1)
    repo_paths: dict[str, tuple[str, ...]]
    excluded_paths: tuple[str, ...]
    prod_paths: tuple[str, ...]
    installed_cell: str
    observed_cell: str
    observed_words: tuple[str, ...] = Field(min_length=1)
    observed_refused: tuple[str, ...]
    min_observed_chars: int = Field(ge=1)
    ruling: str

    @field_validator("observed_refused")
    @classmethod
    def _observed_refused_compiles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"observed_refused pattern does not compile: {exc}"
                ) from exc
        return value


class ModelInstallProofSource(BaseModel):
    """One body source the caller supplies for the install proof."""

    model_config = _FROZEN

    name: str
    text: str


class ModelInstallAncestry(BaseModel):
    """One ancestry answer: the revision, the head and the verdict."""

    model_config = _FROZEN

    rev: str
    head: str
    verdict: Literal["yes", "no", "unread"]
    why: str = ""


class ModelInstallProofRequest(BaseModel):
    """The pr-handoff install-proof request."""

    model_config = _FROZEN

    repo: str
    head: str
    files: tuple[str, ...]
    exempt_kind: str = ""
    sources: tuple[ModelInstallProofSource, ...] = ()
    ancestry: tuple[ModelInstallAncestry, ...] = ()


class ModelInstallProofResult(BaseModel):
    """The pr-handoff install-proof decision."""

    model_config = _FROZEN

    verdict: Literal["OK", "NA", "NO", "REFUSED"]
    detail: str
    required_files: tuple[str, ...] = ()
