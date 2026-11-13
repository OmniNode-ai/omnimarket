# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outbound event on onex.evt.omnimarket.hook-tool-outcome-classified.v1.

Carries the typed outcome of one captured tool call, keyed by the capture
record's ``session_id`` and ``correlation_id``, so a reader joins it to the
stored call without an alias.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_hook_tool_outcome_compute.models.enum_tool_outcome import (
    EnumToolOutcome,
)


class ModelHookToolOutcomeClassified(BaseModel):
    """One classified tool outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["HookToolOutcomeClassified"] = "HookToolOutcomeClassified"
    session_id: str
    correlation_id: str
    tool_name: str
    outcome: EnumToolOutcome
    refusing_component: str | None
    matched_rule: str | None
    exit_code: int | None
    reason: str
    emitted_at: str | None = None


__all__ = ["ModelHookToolOutcomeClassified"]
