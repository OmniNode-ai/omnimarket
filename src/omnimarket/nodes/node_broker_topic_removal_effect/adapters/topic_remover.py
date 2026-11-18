# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The removal port: an in-memory remover that records every call it is given.

There is no real Kafka remover behind this port yet: the runtime wires ``remover=None`` and refuses an
execute run with ``remover_not_wired`` until one is.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    TopicRemovalError,
)


class InMemoryTopicRemover:
    """Removes the names it is given, or refuses with a given reason, recording every call."""

    def __init__(self, fail_with: str | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_with = fail_with

    def remove(self, names: Sequence[str]) -> tuple[str, ...]:
        names = tuple(names)
        self.calls.append(names)
        if self._fail_with is not None:
            raise TopicRemovalError(self._fail_with)
        return names


__all__ = ["InMemoryTopicRemover"]
