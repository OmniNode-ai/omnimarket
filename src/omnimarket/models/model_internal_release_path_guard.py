# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire types for the internal dependency remediation policy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelInternalReleasePathGuardRequest(BaseModel):
    """Only proposed actions belong here; logs and standing rules are evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    proposed_actions: str = Field(min_length=1)
    consumer_scope: Literal["internal", "external"] = "internal"
    dependency_strategy: Literal["unspecified", "git_tag", "git_commit", "release"] = (
        "unspecified"
    )


class ModelInternalReleasePathViolation(BaseModel):
    """One refused action, with its original line for the caller's diagnostic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int = Field(ge=0)
    text: str


class ModelInternalReleasePathGuardResult(BaseModel):
    """A refusal is a typed result, never a release instruction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    exit_code: Literal[0, 1]
    violations: tuple[ModelInternalReleasePathViolation, ...] = ()
    remediation: str = ""
