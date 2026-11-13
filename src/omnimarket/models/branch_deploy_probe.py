# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result models for node_branch_deploy_probe_effect."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelBranchDeployProbeRequest(BaseModel):
    """The typed request to probe the running runtime after a deploy job ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    phase: Literal["deploy", "rollback"]
    expected_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ModelBranchDeployProbeResult(BaseModel):
    """The typed result of probing the running runtime: what it reported, never a verdict.

    ``observed_sha`` is empty when the probe could not be read or when the build
    record named no commit; ``healthy`` is False whenever the probe is not a read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    phase: Literal["deploy", "rollback"]
    status: Literal["read", "unreadable"]
    observed_sha: str = ""
    healthy: bool = False
    expected_sha: str
    detail: str = ""
