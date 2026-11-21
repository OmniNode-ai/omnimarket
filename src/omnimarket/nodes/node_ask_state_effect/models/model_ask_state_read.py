# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request of node_ask_state_effect: where to read and the window (OMN-21713)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelAskStateReadRequest(BaseModel):
    """The capture store, the window start and the time the report is judged at; the ledger rows come from the bus reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    captures_dir: Path
    since: datetime
    now: datetime


__all__ = ["ModelAskStateReadRequest"]
