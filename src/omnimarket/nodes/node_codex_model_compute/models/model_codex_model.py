# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the Codex model choice (OMN-25552)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCodexModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # The id a lane asks Codex to run; None (or blank) asks for the account default.
    codex_model: str | None = None


class ModelCodexModelDeclaration(BaseModel):
    """The node's overlay: the declared Codex model ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    codex_models: tuple[str, ...] = Field(min_length=1)


class ModelCodexModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # False only for an id the declaration does not list
    accepted: bool
    # The id to pass as ``-m``; None means pass no model flag (the account default)
    model: str | None = None
    reason: str
    declared: tuple[str, ...]
