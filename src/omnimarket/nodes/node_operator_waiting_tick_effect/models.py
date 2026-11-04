# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the waiting tick, sharing the canonical renderer input."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_operator_item_render import ModelOperatorItemRenderRequest


class ModelOperatorWaitingRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: ModelOperatorItemRenderRequest
    ledger: str
    resolver: Path
    lane: str
    timeout: float


class ModelOperatorWaiting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[str]
    not_recorded: str | None = None
    exit_code: int = 0
