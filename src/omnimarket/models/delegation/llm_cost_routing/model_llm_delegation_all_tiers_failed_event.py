# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Event model for all model tiers failing for a delegation request."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


class ModelLlmDelegationAllTiersFailedEvent(BaseModel):
    """Event emitted when all model tiers fail for a delegation request.

    Published to the delegation-all-tiers-failed topic declared in contract.yaml.
    This is terminal for a delegation request — caller must handle the error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None
    attempted_models: tuple[str, ...]
    failure_classes: tuple[EnumDelegationFailureClass, ...]
    created_at: datetime
