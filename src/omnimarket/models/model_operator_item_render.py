# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared boundary for rendering the tracked-item fold on every kind of tick."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_ask_state import (
    EnumAskState,
    ModelAskStateReport,
    ModelAskStateRequest,
)


class ModelOperatorItemRenderConfig(BaseModel):
    """Presentation policy supplied by the contract or a deployment overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    words_chars: int = Field(ge=1)
    age_limit_minutes: int = Field(ge=0)
    closed_states: tuple[EnumAskState, ...]
    new_states: tuple[EnumAskState, ...]


class ModelOperatorItemRenderRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    config: ModelOperatorItemRenderConfig | None = None
    report: ModelAskStateReport | None = None
    source_error: str | None = None
    new: frozenset[str] = frozenset()


class ModelOperatorItemRenderResult(BaseModel):
    """Unknown counts stay unknown: an unread source is never an empty report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...]
    known: bool
    count: int | None = None
    unowned: int | None = None
    oldest: str | None = None
    overdue: tuple[str, ...] = ()


class ModelOperatorItemsReadRequest(BaseModel):
    """The live ledger plus the caller's capture/archive snapshot and fold policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path: Path
    fold: ModelAskStateRequest
    config: ModelOperatorItemRenderConfig
    new: frozenset[str] = frozenset()
