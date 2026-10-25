# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and rendered probe for the Codex sandbox check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCodexSandboxProbeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_timeout_s: int
    smoke_timeout_s: int
    bound_fn: str
    private_data: str | None = None


class ModelCodexSandboxProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    preflight_codex_sh: str


class ModelReposPrivateData(BaseModel):
    """The private_data field of the repos overlay; its other fields belong to other readers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    private_data: str | None = None
