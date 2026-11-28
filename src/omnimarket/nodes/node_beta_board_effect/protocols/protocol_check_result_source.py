# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The board's read of the delegation check probe's run events (OMN-26723)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelCheckResultRead,
)


class ProtocolCheckResultSource(Protocol):
    """Gives the run events on a topic published at or after ``since``; a topic that cannot be read comes back as an error, never as no events."""

    def read(self, topic: str, since: datetime) -> ModelCheckResultRead: ...
