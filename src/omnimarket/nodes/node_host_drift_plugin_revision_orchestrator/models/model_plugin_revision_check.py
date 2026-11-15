# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed outcome of the host-drift plugin revision check (OMN-20689)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_plugin_revision import (
    ModelPluginRevisionVerdict,
)


class EnumPluginRevisionCheckOutcome(StrEnum):
    CHECKED = "CHECKED"
    RULES_REFUSED = "RULES_REFUSED"


class ModelPluginRevisionChecked(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumPluginRevisionCheckOutcome
    verdict: ModelPluginRevisionVerdict | None = None
    detail: str = ""
