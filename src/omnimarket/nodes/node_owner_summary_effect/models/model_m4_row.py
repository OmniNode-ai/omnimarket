# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One ledger row that concerns the M4 board."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelM4Row(BaseModel):
    """A TERMINAL or STATUS row whose lane or text names M4 or one of its criteria."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str
    row_type: str
    lane: str
    ticket: str = ""
    outcome: str = ""
    text: str = ""
