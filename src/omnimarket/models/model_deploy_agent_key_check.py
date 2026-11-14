# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result models for the deploy agent key check, and the check value both sides compute."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: The label of the key check value. The signer (node_branch_deploy_command_effect) and the node import ``key_check_value`` from
#: here, so the node and the signer cannot compute different values; tests/nodes/node_deploy_agent_key_check_effect
#: holds the golden vector.
KEY_CHECK_LABEL: bytes = b"deploy-agent-key-check-v1"


def key_check_value(secret: str) -> str:
    """The one-way check value of a signing key: 16 lowercase hex characters, safe to name in a refusal."""
    return hmac.new(secret.encode(), KEY_CHECK_LABEL, hashlib.sha256).hexdigest()[:16]


class ModelDeployAgentKeyCheckRequest(BaseModel):
    """The typed request for the deploy agent's expected key check value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    requested_by: str = Field(min_length=1)


class ModelDeployAgentKeyCheckResult(BaseModel):
    """The typed answer: the check value when served, the reason when refused; never the key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    status: Literal["served", "refused"]
    key_check: str = ""
    detail: str = ""
