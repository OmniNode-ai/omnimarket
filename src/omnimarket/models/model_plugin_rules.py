# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the host-drift plugin rules overlay (OMN-20689)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_plugin_revision import (
    ModelPluginSyncRule,
)


class EnumPluginRulesOutcome(StrEnum):
    LOADED = "LOADED"
    REFUSED = "REFUSED"


class ModelPluginRulesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)


class ModelPluginRulesResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumPluginRulesOutcome
    rules: tuple[ModelPluginSyncRule, ...] = ()
    detail: str = ""
