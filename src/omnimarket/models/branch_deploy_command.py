# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result models for node_branch_deploy_command_effect."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelBranchDeployCommandRequest(BaseModel):
    """The typed request to sign and publish the deploy agent's rebuild command over the bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: Literal["deploy", "rollback"]
    runtime_lane: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    pr_number: int = Field(ge=1)
    git_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    image_ref: str = ""
    overlay_ref: str = Field(default="", pattern=r"^([0-9a-f]{40})?$")
    rollback_actor: str = Field(min_length=1)
    rollback_reason: str = Field(min_length=1)
    requested_by: str = Field(min_length=1)


class ModelBranchDeployCommandResult(BaseModel):
    """The typed result of publishing the deploy agent's rebuild command over the bus (no trigger)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    mode: Literal["deploy", "rollback"]
    status: Literal["published", "refused", "failed"]
    detail: str = ""
    # Set only on a key mismatch refusal (OMN-24396): the one-way check values of both keys, never a key.
    signer_key_check: str = ""
    agent_key_check: str = ""
