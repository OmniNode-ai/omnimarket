# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus ticket writer reads the completion out of a draft write's answer pair (OMN-26715).

Exemplar: test_regression_ticket.py (the writer's caller) and node_linear_ticket_write_effect's draft route.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.models.ticket_write_io import ModelTicketWriteCompleted
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_regression_ticket import (
    BusTicketWriter,
)


def _completed(request_status: str) -> ModelTicketWriteCompleted:
    return ModelTicketWriteCompleted(
        correlation_id=uuid.uuid4(),
        operation=EnumTicketOperation.CREATE,
        idempotency_key="k",
        requested_by_lane="lane",
        result=ModelCreateTicketResult(status=request_status),
    )


class _Node:
    def __init__(self, answer: object) -> None:
        self._answer = answer

    async def handle(self, command: object) -> object:
        return self._answer


def _writer(answer: object) -> BusTicketWriter:
    writer = BusTicketWriter.__new__(BusTicketWriter)
    writer._lane = "lane"
    writer._node = _Node(answer)  # type: ignore[assignment]
    return writer


def _request() -> ModelCreateTicketRequest:
    return ModelCreateTicketRequest(title="t", description="d")


def test_a_pair_answer_yields_its_completion() -> None:
    pair = (SimpleNamespace(), _completed("created"))
    assert _writer(pair).send(_request()).status == "created"


def test_a_request_carrying_its_own_correlation_id_is_sent() -> None:
    request = ModelCreateTicketRequest(
        title="t", description="d", correlation_id=uuid.uuid4()
    )
    assert _writer(_completed("created")).send(request).status == "created"


def test_a_single_completion_still_yields_its_result() -> None:
    assert _writer(_completed("created")).send(_request()).status == "created"


def test_a_pair_without_completion_is_refused() -> None:
    with pytest.raises(RuntimeError, match="no completion"):
        _writer((SimpleNamespace(), SimpleNamespace())).send(_request())
