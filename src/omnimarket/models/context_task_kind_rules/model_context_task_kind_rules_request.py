# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request: a resolved profile and the caller scope its task-kind rules read."""

from __future__ import annotations

from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.pack.model_context_profile import ModelContextProfile


class ModelContextTaskKindRulesRequest(BaseModel):
    """The profile's name and data, and the caller scope passed through unchanged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str = Field(min_length=1)
    profile: ModelContextProfile
    caller_scope: ModelContextCallerScope
    request_id: str = ""


__all__ = ["ModelContextTaskKindRulesRequest"]
