# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lane that wrote a TERMINAL row in the window."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLaneClosed(BaseModel):
    """A closed lane with its outcome and delegation cells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    lane: str
    ticket: str = ""
    outcome: str = ""
    pr: str = ""
    delegated: str = ""
    codex: bool = False
    text: str = ""
