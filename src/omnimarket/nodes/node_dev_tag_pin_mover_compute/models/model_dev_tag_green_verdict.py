# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the dev-tag green verdict: one commit's check runs and dev's required checks."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumDevTagGreenVerdict(StrEnum):
    """green; red; unread (a required check still running, or none ran)."""

    GREEN = "green"
    RED = "red"
    UNREAD = "unread"


class ModelDevTagCheckRun(BaseModel):
    """One check run on a dev merge commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: str
    conclusion: str = ""


class ModelDevTagGreenVerdictRequest(BaseModel):
    """The commit's check runs and dev's required contexts (None when unreadable)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: tuple[ModelDevTagCheckRun, ...]
    required_contexts: frozenset[str] | None


class ModelDevTagGreenVerdictResult(BaseModel):
    """The verdict on one dev merge commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumDevTagGreenVerdict


__all__ = [
    "EnumDevTagGreenVerdict",
    "ModelDevTagCheckRun",
    "ModelDevTagGreenVerdictRequest",
    "ModelDevTagGreenVerdictResult",
]
