# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host-owned Codex attempt; missing measurements remain unknown."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCodexHarnessRequest(BaseModel):
    """Only the task, requested model and deadline cross the caller seam."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    prompt: str = Field(min_length=1, repr=False)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class ModelCodexHarnessPolicy(BaseModel):
    """Contract defaults plus the host overlay; no credentials or auth override."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    executable: str = Field(default="codex", min_length=1)
    workdir: Path = Field(default_factory=Path.cwd)
    sandbox: Literal["workspace-write", "read-only"] = "workspace-write"
    approval_policy: Literal["never"] = "never"
    network_access: bool = True
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "high"
    max_timeout_seconds: float = Field(default=600, gt=0, allow_inf_nan=False)
    terminate_grace_seconds: float = Field(default=5, gt=0, le=30)

    @field_validator("workdir")
    @classmethod
    def absolute_workdir(cls, value: Path) -> Path:
        """Keep child output paths correct when an overlay uses a relative root."""
        return value.resolve()


class ModelCodexHarnessReceipt(BaseModel):
    """Answer and measured counters only; never raw prompts or diagnostics."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    answer: str = Field(default="", repr=False)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    tokens_cached: int | None = Field(default=None, ge=0)
    wall_seconds: float = Field(ge=0, allow_inf_nan=False)
    # CLI model selection is explicit. Codex JSONL does not attest an observed model.
    model: str
    model_source: Literal["requested"] = "requested"
    exit_cause: Literal[
        "completed",
        "startup_refused",
        "launch_failed",
        "timeout",
        "process_failed",
        "turn_failed",
        "invalid_output",
    ]
    exit_code: int | None = None
