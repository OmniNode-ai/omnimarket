# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lane that wrote a CLAIM in the window and has not closed."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLaneStarted(BaseModel):
    """A lane claimed in the window with no TERMINAL row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    lane: str
    ticket: str = ""
