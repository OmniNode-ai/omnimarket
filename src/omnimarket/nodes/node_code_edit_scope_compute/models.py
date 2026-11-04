# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed wire inputs and outputs for deterministic code-edit scope derivation."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEditScopeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str
    tracked_files: Sequence[str]
    python: str


class ModelCodeEditScopeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    writable: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
