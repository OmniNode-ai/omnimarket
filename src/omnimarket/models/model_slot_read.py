# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest read of a scheduled source, kept beside its slot reads so a step can serve it while the current slot's run is unfinished."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelSlotReadEnvelope(BaseModel):
    """One source read with the slot it was taken in and the moment the source was last checked for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: datetime
    checked_at: datetime
    read_json: str
