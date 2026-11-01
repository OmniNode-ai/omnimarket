# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared request and receipt of one Claude Code credential probe (OMN-21626).

The probe asks whether the credential in the host's environment still answers. It is placed by
``node_llm_delegation_call_effect``, the sanctioned seam for a model call, and read by
``node_claude_pool_refill_effect``, so both import it from here and neither reaches into the other's
models package. No field carries a credential: the credential travels in the host's environment only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelClaudeCredentialProbeRequest(BaseModel):
    """The smallest call that proves a credential: one prompt, one model, one deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    prompt: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class ModelClaudeCredentialProbeReceipt(BaseModel):
    """What the host's Claude Code answered, with every ``sk-ant-`` string redacted and the text bounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: str = ""
    wall_seconds: float = Field(ge=0, allow_inf_nan=False)
    model: str
    exit_cause: Literal["answered", "timeout", "launch_failed", "process_failed"]
    exit_code: int | None = None


__all__ = ["ModelClaudeCredentialProbeReceipt", "ModelClaudeCredentialProbeRequest"]
