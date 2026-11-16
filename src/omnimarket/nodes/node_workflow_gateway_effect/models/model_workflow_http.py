# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller, response and command models of the workflow gateway HTTP surface."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowCaller(BaseModel):
    """The authenticated tenant principal that issued a gateway command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    tenant_slug: str | None = None
    principal_id: str = Field(..., min_length=1)
    authenticated_user_id: str = Field(..., min_length=1)


class ModelWorkflowHttpResponse(BaseModel):
    """Status code and JSON body the gateway answers with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: Any
    headers: dict[str, str] = Field(default_factory=dict)


class ModelWorkflowSubmitCommand(BaseModel):
    """A submission: the caller plus the already-decoded JSON body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: ModelWorkflowCaller
    body: Any


class ModelWorkflowStatusCommand(BaseModel):
    """A status lookup for one workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: ModelWorkflowCaller
    workflow_id: str


class ModelWorkflowContractsCommand(BaseModel):
    """A read of the servable workflow catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: ModelWorkflowCaller


class ModelWorkflowReceiptCommand(BaseModel):
    """A receipt fetch for one workflow, optionally pinned to a runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: ModelWorkflowCaller
    workflow_id: str
    runner_identity: str | None = None


__all__: list[str] = [
    "ModelWorkflowCaller",
    "ModelWorkflowContractsCommand",
    "ModelWorkflowHttpResponse",
    "ModelWorkflowReceiptCommand",
    "ModelWorkflowStatusCommand",
    "ModelWorkflowSubmitCommand",
]
