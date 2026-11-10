# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the landing snapshot step (OMN-23409): its two declared sources, its recorder and its publisher."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelLandingTickRecord,
    ModelPrWriteFacts,
)


class ProtocolLandingFactsSource(Protocol):
    """The tick's facts as the watcher state and the ledger give them; None when unread."""

    name: str

    def read_facts(self, tick: int) -> ModelLandingFacts | None: ...


class ProtocolLandingWriteFactsSource(Protocol):
    """The per-PR node id and the head's failed run ids, one batched read for the tick's PRs at their heads."""

    name: str

    def read_write_facts(self, heads: dict[str, str]) -> dict[str, ModelPrWriteFacts]:
        """PR key to head sha in; PR key to its write facts out; a PR it cannot read is absent."""
        ...


class ProtocolLandingTickRecorder(Protocol):
    """Keeps every tick's record; a record is never replaced by a later tick's."""

    def record(self, record: ModelLandingTickRecord) -> None: ...

    def recorded(self) -> tuple[ModelLandingTickRecord, ...]: ...


class ProtocolLandingSnapshotPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


__all__: list[str] = [
    "ProtocolLandingFactsSource",
    "ProtocolLandingSnapshotPublisher",
    "ProtocolLandingTickRecorder",
    "ProtocolLandingWriteFactsSource",
]
