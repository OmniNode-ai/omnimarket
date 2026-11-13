# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols the seam counter takes by constructor injection (OMN-21849)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    ModelSeamCountedEvent,
    ModelSeamRefusal,
    ModelSeamRowRef,
    ModelSeamWindow,
)


class ProtocolSeamSources(Protocol):
    """The three sides of the seam: the ledger file, the topics, the projection."""

    def file_rows(self, window: ModelSeamWindow) -> Sequence[ModelSeamRowRef]: ...

    def topic_row_ids(self, window: ModelSeamWindow) -> Sequence[str]: ...

    def projection_row_ids(self, window: ModelSeamWindow) -> Sequence[str]: ...


class ProtocolRowIdReader(Protocol):
    """Reads the row ids one side of the seam holds for a window."""

    def row_ids(self, window: ModelSeamWindow) -> Sequence[str]: ...


class ProtocolRefusalSource(Protocol):
    """File rows the bus path refused, each with the reason."""

    def refusals(self, window: ModelSeamWindow) -> Sequence[ModelSeamRefusal]: ...


class ProtocolPendingReplaySource(Protocol):
    """Row ids queued for replay (the file-mode replay queue satisfies this)."""

    def pending_row_ids(self) -> set[str]: ...


class ProtocolSeamEventPublisher(Protocol):
    """Publishes the per-window count event on a topic the contract declares."""

    def publish(self, topic: str, event: ModelSeamCountedEvent) -> None: ...
