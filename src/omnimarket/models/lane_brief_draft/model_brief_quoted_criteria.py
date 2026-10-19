# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Criterion lines of a ticket, quoted by the shell exactly as the live ticket holds them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBriefQuotedCriteria(BaseModel):
    """Rendered as a `ticket-criteria: <ticket>` line and the lines prefixed `> `."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    lines: tuple[str, ...] = Field(min_length=1)
