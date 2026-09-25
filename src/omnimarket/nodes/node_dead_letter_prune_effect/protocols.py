# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dead-letter store boundary. The sink and cipher are the topic archive's
(omnimarket.topic_archive.protocols), so both archives land in one place under
one encryption scheme."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from omnimarket.nodes.node_dead_letter_prune_effect.models import (
    ModelDeadLetterDay,
    ModelDeadLetterRow,
)


class ProtocolDeadLetterStore(Protocol):
    """event_ledger's dead-letter rows. Deletes name exact keys, never a range."""

    def list_days(
        self, *, cutoff: dt.date, topic_like: str
    ) -> list[ModelDeadLetterDay]:
        """Every (topic, partition, UTC day) strictly before ``cutoff``, oldest first."""
        ...

    def read_chunk(
        self, *, topic: str, partition: int, day: dt.date, after_offset: int, limit: int
    ) -> list[ModelDeadLetterRow]:
        """Up to ``limit`` rows of that day with offset > ``after_offset``, by offset."""
        ...

    def window_offsets(
        self,
        *,
        topic: str,
        partition: int,
        day: dt.date,
        first_offset: int,
        last_offset: int,
    ) -> list[int]:
        """The offsets that day holds between the two bounds, inclusive, sorted."""
        ...

    def delete_offsets(self, *, topic: str, partition: int, offsets: list[int]) -> int:
        """Delete exactly these (topic, partition, offset) keys in one committed
        transaction; return how many rows were deleted."""
        ...
