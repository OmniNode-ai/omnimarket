# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of one handshake policy gate run (OMN-20671)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_handshake_policy_gate_compute.models.model_handshake_policy_gate import (
    EnumPolicyGateVerdict,
)


class ModelPolicyGateRunRequest(BaseModel):
    """One run of the gate: the repos.conf text, strictness and the two raw retry tunables."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos_conf_text: str
    strict: bool = False
    max_attempts_raw: str | None = None
    base_delay_raw: str | None = None


class ModelPolicyGateRunResult(BaseModel):
    """What the run printed and returned, plus the endpoints it read and the sleeps it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stdout: str
    stderr: str
    exit_code: int = Field(ge=0, le=2)
    verdict: EnumPolicyGateVerdict | None = None
    endpoints: list[str] = Field(default_factory=list)
    sleeps: list[int] = Field(default_factory=list)
