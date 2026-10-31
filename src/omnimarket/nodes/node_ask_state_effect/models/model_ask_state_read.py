# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request of node_ask_state_effect: where to read and the window (OMN-21713)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelAskStateReadRequest(BaseModel):
    """The capture store, the ledger file, the window start and the time the report is judged at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    captures_dir: Path
    ledger_path: Path
    since: datetime
    now: datetime
    archives: bool = True


__all__ = ["ModelAskStateReadRequest"]
