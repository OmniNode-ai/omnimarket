# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger reducer state (OMN-8950).

Pure state shape — no I/O, no bus. Accumulates tick_count, last seen hash,
and last line count from successive hash-computed events.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLedgerState(BaseModel):
    """Accumulated ledger projection state."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tick_count: int = 0
    last_hash: str = ""
    last_line_count: int = 0
