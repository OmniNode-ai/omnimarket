# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The consumer-flow store boundary. The sink and cipher are the topic archive's
(omnimarket.topic_archive.protocols), so this archive lands next to the
dead-letter and topic archives under one encryption scheme."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from omnimarket.nodes.node_consumer_flow_prune_effect.models import (
    ModelConsumerFlowDay,
    ModelConsumerFlowRow,
)


class ProtocolConsumerFlowStore(Protocol):
    """consumer_flow_windows rows by UTC day of window_start, keyed on
    projection_cursor. Deletes name exact cursors, never a range, and are
    bounded to the named day as well."""

    def list_days(self, *, cutoff: dt.date) -> list[ModelConsumerFlowDay]:
        """Every UTC day strictly before ``cutoff`` that holds rows, oldest first."""
        ...

    def day_cursors(self, *, day: dt.date) -> list[int]:
        """Every projection_cursor whose window_start falls on ``day``, sorted."""
        ...

    def read_rows(
        self, *, day: dt.date, cursors: list[int]
    ) -> list[ModelConsumerFlowRow]:
        """The rows among ``cursors`` whose window_start still falls on ``day``,
        by cursor."""
        ...

    def delete_cursors(self, *, day: dt.date, cursors: list[int]) -> int:
        """Delete exactly these cursors, and only where window_start falls on
        ``day``, in one committed transaction; return how many were deleted."""
        ...
