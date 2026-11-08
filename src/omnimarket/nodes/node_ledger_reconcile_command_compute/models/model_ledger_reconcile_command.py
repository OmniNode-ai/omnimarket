# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied ledger reconcile execution facts and the resulting command plan."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLedgerReconcileCommandRequest(BaseModel):
    """Resolve a command from facts; argv is already selected by the caller.

    Supply either project_path or resolve_error. uv_cache_dir is the existing
    environment value, including an empty string if set but empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve"] = "resolve"
    project_path: str | None = None
    resolve_error: str | None = None
    uv_path: str | None
    uv_cache_dir: str | None = None
    tempdir_path: str
    argv: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_resolution_fact(self) -> Self:
        if self.project_path is None and self.resolve_error is None:
            raise ValueError("requires project_path or resolve_error")
        return self


class ModelLedgerReconcileCommandResult(BaseModel):
    """Command and environment changes, or the original wrapper's refusal.

    exit_code zero means the caller should run command. env_overrides contains
    UV_CACHE_DIR only when the wrapper would set it; other environment stays
    with the caller. No command runs and no directory is created here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve"] = "resolve"
    command: list[str] = Field(default_factory=list)
    env_overrides: dict[str, str] = Field(default_factory=dict)
    exit_code: Literal[0, 2, 127] = 0
    message: str | None = None
