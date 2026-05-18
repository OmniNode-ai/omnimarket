# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for OmniGate check execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCheckExecutorInput(BaseModel):
    """Input for executing trusted OmniGate checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_path: str = Field(description="Path to the trusted .omnigate.yaml file.")
    repo_path: str = Field(description="Path to the repository root.")


class ModelOmniGateNodeCheckResult(BaseModel):
    """Serializable check result returned by the node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    command: str = ""
    status: str
    duration_ms: int = Field(default=0, ge=0)
    stdout_preview: str | None = None
    stdout_hash: str | None = None


class ModelCheckExecutorResult(BaseModel):
    """Output from the check executor node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[ModelOmniGateNodeCheckResult, ...] = Field(default=())
    all_passed: bool


__all__ = [
    "ModelCheckExecutorInput",
    "ModelCheckExecutorResult",
    "ModelOmniGateNodeCheckResult",
]
