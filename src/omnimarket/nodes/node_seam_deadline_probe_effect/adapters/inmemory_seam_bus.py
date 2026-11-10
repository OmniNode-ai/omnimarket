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
    the deadline. A latency past the deadline but inside the demand's ``late_listen_seconds``
    listen window is the planted late reply: the answer records when it came, but the crossing
    stays ``no-result-by-deadline``. A topic with no responder is a command nobody consumes.
    ``unreadable`` makes every demand ``bus-unreadable``. ``sent`` records every demand, in order.
    ``send_ack_ms`` is how long the fixture broker takes to acknowledge a send; every answer
    carries it except the bus-unreadable one.
    """

    def __init__(
        self,
        responders: dict[str, Responder] | None = None,
        *,
        latency_ms: int = 5,
        send_ack_ms: int = 2,
    ) -> None:
        self.responders: dict[str, Responder] = dict(responders or {})
        self.latency_ms = latency_ms
        self.send_ack_ms = send_ack_ms
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
        deadline_ms = demand.deadline_seconds * 1000
        listen_ms = deadline_ms + demand.late_listen_seconds * 1000
        error = answer is not None and any(answer.get(key) for key in demand.error_keys)
        if answer is None or self.latency_ms > deadline_ms:
            # A late reply is recorded, never excused: the crossing does not change.
            late = answer is not None and not error and self.latency_ms <= listen_ms
            return ModelSeamAnswer(
                crossing=EnumSeamCrossing.NO_RESULT_BY_DEADLINE,
                detail="late" if late else "",
                late_reply_ms=self.latency_ms if late else None,
                send_ack_ms=self.send_ack_ms,
            )
        if error:
            return ModelSeamAnswer(
                crossing=EnumSeamCrossing.RESULT_ERROR,
                elapsed_ms=self.latency_ms,
                send_ack_ms=self.send_ack_ms,
            )
        return ModelSeamAnswer(
            crossing=EnumSeamCrossing.RESULT,
            elapsed_ms=self.latency_ms,
            send_ack_ms=self.send_ack_ms,
        )


__all__ = ["InMemorySeamBus", "Responder"]
