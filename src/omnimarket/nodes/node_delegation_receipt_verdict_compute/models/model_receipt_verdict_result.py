# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A typed client verdict and exactly one Claude Code hook output shape."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation.model_delegation_client_verdict import (
    ModelDelegationClientVerdictResult,
)


class ModelReceiptVerdictResult(BaseModel):
    """No terminal content is returned unless its identity and gate were verified."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    client_verdict: ModelDelegationClientVerdictResult
    hook_output: dict[str, str] | None = None
