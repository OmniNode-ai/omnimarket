# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick: the clock moved, so a window or a roll bound may have ended."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelDevRollGateTick(BaseModel):
    """Topic ``onex.cmd.omnimarket.dev-roll-gate-tick.v1``; the gate reads its own clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tick_id: UUID
