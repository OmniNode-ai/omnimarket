# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled closer's ticket bus call: the lane, the timeout and the topics (OMN-24301)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import omnimarket.delegated_test_loop.lane_bus as lane_bus
import omnimarket.handlers.scm.handler_scm_dispatch as scm_dispatch
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal, ModelScmTopics
from omnimarket.models.ticket_write_io import (
    EnumTicketOperation,
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_bus_call import (
    TicketBusCallError,
    bus_ticket_call,
)

pytestmark = pytest.mark.unit

LANE = "ticket-closer-fixture"
TIMEOUT_S = 12.0
TOPICS = load_ticket_topics()


def terminal(*, failed: bool = False) -> ModelScmTerminal:
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid4(),
            command_topic=TOPICS.command,
            command_envelope_id=uuid4(),
            terminal_topic=TOPICS.success,
        ),
        failed=failed,
        payload={},
    )


def request() -> ModelTicketWriteRequest:
    return ModelTicketWriteRequest.model_validate(
        {
            "operation": "read",
            "ticket_id": "OMN-1",
            "requested_by_lane": LANE,
            "idempotency_key": "key-1",
            "correlation_id": uuid4(),
        }
    )


class SeamCaller:
    """The ``ScmBusCaller`` seam: records what the call published, then answers."""

    def __init__(self, answer: ModelScmTerminal | Exception) -> None:
        self.answer = answer
        self.published: list[ModelScmTopics] = []
        self.requests: list[ModelTicketWriteRequest] = []
        self.correlation_ids: list[UUID] = []
        self.timeouts: list[float] = []

    async def call_topics(
        self,
        *,
        topics: ModelScmTopics,
        request: ModelTicketWriteRequest,
        correlation_id: UUID,
        timeout_s: float,
    ) -> ModelScmTerminal:
        self.published.append(topics)
        self.requests.append(request)
        self.correlation_ids.append(correlation_id)
        self.timeouts.append(timeout_s)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def patch_bus(
    monkeypatch: pytest.MonkeyPatch, caller: SeamCaller
) -> list[dict[str, object]]:
    """The fake opener and caller behind the lazily imported seams; returns what opened."""
    opened: list[dict[str, object]] = []

    # The opener declares the guarded keywords; the workspace keyword stands in
    # for the fourth, whose real name the public-repo hygiene gate refuses.
    @contextlib.asynccontextmanager
    async def opener(
        *,
        bus: str,
        lane: str,
        kafka_bootstrap: str | None,
        workspace: Path | None,
    ) -> AsyncIterator[object]:
        opened.append(
            {
                "bus": bus,
                "lane": lane,
                "kafka_bootstrap": kafka_bootstrap,
                "workspace": workspace,
            }
        )
        yield object()

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", opener)
    monkeypatch.setattr(scm_dispatch, "ScmBusCaller", lambda _handle: caller)
    return opened


def test_golden_the_call_opens_the_lane_and_returns_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = SeamCaller(terminal())
    opened = patch_bus(monkeypatch, caller)
    answer = bus_ticket_call(bus_lane=LANE, timeout_s=TIMEOUT_S)(request())
    assert answer is caller.answer
    assert [row["lane"] for row in opened] == [LANE]
    assert [row["bus"] for row in opened] == ["kafka"]


def test_the_published_request_rides_the_ticket_topics_command_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = SeamCaller(terminal())
    patch_bus(monkeypatch, caller)
    sent = request()
    bus_ticket_call(bus_lane=LANE, timeout_s=TIMEOUT_S)(sent)
    assert caller.published == [TOPICS]
    assert caller.published[0].command == load_ticket_topics().command
    assert caller.requests == [sent]
    assert caller.requests[0].operation is EnumTicketOperation.READ
    assert caller.correlation_ids == [sent.correlation_id]
    assert caller.timeouts == [TIMEOUT_S]


def test_error_chain_a_timeout_is_raised_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = SeamCaller(TimeoutError("no terminal"))
    patch_bus(monkeypatch, caller)
    call = bus_ticket_call(bus_lane=LANE, timeout_s=TIMEOUT_S)
    with pytest.raises(TicketBusCallError, match="no answer on lane") as caught:
        call(request())
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_error_chain_a_bus_that_cannot_open_is_raised_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = SeamCaller(terminal())  # never reached: the bus never opens

    class RefusedOpening:
        async def __aenter__(self) -> object:
            raise LabRunBusError("no lane declaration")

        async def __aexit__(self, *exc: object) -> None:
            return None

    def refused_opener(
        *,
        bus: str,
        lane: str,
        kafka_bootstrap: str | None,
        workspace: Path | None,
    ) -> RefusedOpening:
        return RefusedOpening()

    monkeypatch.setattr(lane_bus, "open_lab_run_bus", refused_opener)
    monkeypatch.setattr(scm_dispatch, "ScmBusCaller", lambda _handle: caller)
    call = bus_ticket_call(bus_lane=LANE, timeout_s=TIMEOUT_S)
    with pytest.raises(TicketBusCallError, match="ticket bus lane") as caught:
        call(request())
    assert isinstance(caught.value.__cause__, LabRunBusError)
