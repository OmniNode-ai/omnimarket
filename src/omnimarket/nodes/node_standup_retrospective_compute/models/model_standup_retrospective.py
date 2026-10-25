# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the stand-up retrospective node."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelStandupRetrospectiveRequest(BaseModel):
    """The window's end (ISO 8601) and the text of the ``--retro`` entries file, None when the flag was not given.

    The caller reads the file; an unreadable file is the caller's ``retrospective:`` error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    until: str
    entries: str | None = None


class ModelStandupRetrospectiveResult(BaseModel):
    """``retrospective`` is empty on any day but Friday; ``errors`` carry the ``retrospective:`` source prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retrospective: dict[str, Any] = Field(default_factory=dict)
    ticket_drafts: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
