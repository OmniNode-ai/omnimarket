# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Delegation event model emitted by the delegation orchestrator."""

from __future__ import annotations

from omnibase_compat.contracts.delegation.wire import ModelDelegationResult
from pydantic import BaseModel, ConfigDict


class ModelDelegationEvent(BaseModel):
    """Event emitted when the orchestrator completes or fails a delegation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    payload: ModelDelegationResult


__all__: list[str] = ["ModelDelegationEvent"]
