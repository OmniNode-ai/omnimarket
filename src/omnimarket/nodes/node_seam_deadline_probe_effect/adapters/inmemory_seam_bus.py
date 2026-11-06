# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory seam bus: a fixture responder per command topic, the default backend."""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
    ModelSeamAnswer,
    ModelSeamDemand,
)

#: A responder takes the command payload and returns the answer payload, or None for no answer.
Responder = Callable[[dict[str, str | int | bool]], dict[str, object] | None]


class InMemorySeamBus:
    """Answers each demand from the responder of its command topic.

    ``latency_ms`` is how long the fixture node takes; a latency past the deadline is no result by
    the deadline. A topic with no responder is a command nobody consumes. ``unreadable`` makes every
    demand ``bus-unreadable``. ``sent`` records every demand, in order.
    """

    def __init__(
        self,
        responders: dict[str, Responder] | None = None,
        *,
        latency_ms: int = 5,
    ) -> None:
        self.responders: dict[str, Responder] = dict(responders or {})
        self.latency_ms = latency_ms
        self.unreadable: str | None = None
        self.sent: list[ModelSeamDemand] = []

    def demand(self, demand: ModelSeamDemand) -> ModelSeamAnswer:
        self.sent.append(demand)
        if self.unreadable is not None:
            return ModelSeamAnswer(
                crossing=EnumSeamCrossing.BUS_UNREADABLE, detail=self.unreadable
            )
        responder = self.responders.get(demand.command_topic)
        answer = responder(dict(demand.payload)) if responder is not None else None
        if answer is None or self.latency_ms > demand.deadline_seconds * 1000:
            return ModelSeamAnswer(crossing=EnumSeamCrossing.NO_RESULT_BY_DEADLINE)
        if any(answer.get(key) for key in demand.error_keys):
            return ModelSeamAnswer(
                crossing=EnumSeamCrossing.RESULT_ERROR, elapsed_ms=self.latency_ms
            )
        return ModelSeamAnswer(
            crossing=EnumSeamCrossing.RESULT, elapsed_ms=self.latency_ms
        )


__all__ = ["InMemorySeamBus", "Responder"]
