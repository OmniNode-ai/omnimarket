# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A source-row reference, never an agent-authored probe command."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..tools.context import ModelMorningToolContext


class ModelMorningRuntimeProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    source_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="source_sha256 returned by kb_read_source_doc; stale sources refuse",
    )
    source_line: int = Field(
        ge=1, le=100_000, description="one-based line of the source table's row"
    )


class ModelMorningRuntimeProbeCommand(ModelMorningRuntimeProbeRequest):
    """The phase server supplies trusted context outside the agent's tool input."""

    context: ModelMorningToolContext


class ModelMorningRuntimeProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc: str
    source_line: int
    source_copy: str
    source_sha256: str
    row_sha256: str
    source_command: str
    done_proof: str
    host: str
    observed_at: str
    executed: bool
    exit: int | None
    stdout: str
    stderr: str
    assertion_passed: bool | None = None
    truncated: bool = False
