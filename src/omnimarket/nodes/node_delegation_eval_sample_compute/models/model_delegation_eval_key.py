# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stable attempt identity without prompt or response content."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDelegationEvalKey(BaseModel):
    """Stable attempt identity without prompt or response content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    attempt_index: int
