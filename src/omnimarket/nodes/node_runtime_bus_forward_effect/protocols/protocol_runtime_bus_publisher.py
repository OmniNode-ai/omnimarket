# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publisher seam of node_runtime_bus_forward_effect (OMN-24003).

The forward handler owns every publish this node makes to the hosted bus the node's overlay declares; this protocol is
the only wire it sends them on. The live implementation dials the broker the overlay resolves; the recorded fake in the
unit tests replays what the bus took.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class PublisherRefusedError(Exception):
    """The hosted bus client refused a publish this node asked it to make."""


@runtime_checkable
class ProtocolRuntimeBusPublisher(Protocol):
    """Carry one run's recorded events to the hosted bus."""

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        """Hand one event to the client; raise :class:`PublisherRefusedError` when it refuses the publish."""
        ...

    def flush(self, timeout_s: float) -> int:
        """Wait up to ``timeout_s`` for delivery; return how many published events were not confirmed delivered."""
        ...

    def failure_detail(self) -> str | None:
        """The first reason the hosted bus gave for an event it did not confirm; ``None`` when it gave none."""
        ...
