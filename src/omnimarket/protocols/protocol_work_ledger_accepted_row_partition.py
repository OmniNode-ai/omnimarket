# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The accepted-row partition and the old row topics, as the relay and republish handlers see them (OMN-25630)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.models.model_work_ledger_accepted_row import ModelAcceptedRowEvent


class ProtocolAcceptedRowPartition(Protocol):
    """The one partition of the accepted-row topic: its offset order is the ledger order.

    ``read`` returns every event already on the partition in offset order, so a restarted relay
    resumes from what the topic holds and never from a memory of its own.
    """

    def read(self) -> Sequence[ModelAcceptedRowEvent]: ...

    def publish(self, event: ModelAcceptedRowEvent) -> None: ...


class ProtocolOldRowTopics(Protocol):
    """The 22 old row topics as the republish command writes to them."""

    def row_ids_on(self, topic: str) -> frozenset[str]: ...

    def publish(self, topic: str, row_id: str, payload: dict[str, object]) -> None: ...


__all__: list[str] = ["ProtocolAcceptedRowPartition", "ProtocolOldRowTopics"]
