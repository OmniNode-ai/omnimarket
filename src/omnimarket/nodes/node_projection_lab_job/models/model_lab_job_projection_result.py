# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's output: the complete state upsert and transition append."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLabJobProjectionResult(BaseModel):
    """Every event derives one state row dict and one transition row dict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_row: dict[str, Any] = Field(...)
    transition_row: dict[str, Any] = Field(...)


__all__: list[str] = ["ModelLabJobProjectionResult"]
