# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of the session stall alarm: the transcript's rows, read from wherever the host keeps them."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.models.session_stall import ModelQueueRow


class TranscriptUnreadableError(Exception):
    """The transcript could not be read; the tick is INDETERMINATE (neither a stall nor a recovery)."""


class ProtocolTranscriptRows(Protocol):
    def read(self, now: datetime) -> tuple[ModelQueueRow, ...]:
        """The queue rows of the session's transcript inside the reader's window ending at ``now``."""
        ...


__all__ = ["ProtocolTranscriptRows", "TranscriptUnreadableError"]
