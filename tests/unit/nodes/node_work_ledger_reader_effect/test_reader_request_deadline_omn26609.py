# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader stamps the deadline it will wait to on every request it sends (OMN-26609).

The answerer drops a request whose deadline passed (see the query node's deadline test). The
callers are the reader's two routes: the command route (one ``onex ledger query --stdin`` process
per page) and the bus session route. Each waits ``timeout_seconds`` for an answer, so the deadline
it stamps is send time plus that wait. Stamping is a declared route field (``stamp_deadline``),
off unless the overlay turns it on, because an answerer that predates the deadline field refuses a
request that carries it: the overlay turns it on only once the dev runtime serves the field.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    BusSessionPointRead,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelBusSessionReadRoute,
    ModelCommandReadRoute,
    ModelPointQuery,
)

pytestmark = pytest.mark.unit

SENT = datetime(2026, 11, 23, 16, 20, 0, tzinfo=UTC)
WAIT_S = 121


def _clock() -> datetime:
    return SENT


class RecordingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def __aenter__(self) -> RecordingClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def query(
        self, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "correlation_id": request["correlation_id"],
            "query": request["query"],
            "status": "ok",
            "matched": 0,
            "open_claims": [],
        }


def _bus_read(*, stamp: bool) -> tuple[BusSessionPointRead, RecordingClient]:
    client = RecordingClient()
    port = BusSessionPointRead(
        lambda: client,
        WAIT_S,
        new_id=lambda: str(uuid.uuid4()),
        stamp_deadline=stamp,
        clock=_clock,
    )
    return port, client


def test_bus_session_stamps_send_time_plus_the_wait_it_will_make() -> None:
    port, client = _bus_read(stamp=True)
    port.read(ModelPointQuery(query="open-claims"))
    port.close()
    sent = client.requests[0]
    assert datetime.fromisoformat(sent["deadline"]) == SENT + timedelta(seconds=WAIT_S)
    # The stamped request is one the answerer's own model accepts.
    assert ModelWorkLedgerQueryRequest.model_validate(
        sent
    ).deadline == SENT + timedelta(seconds=WAIT_S)


def test_bus_session_stamps_nothing_when_the_route_does_not_declare_it() -> None:
    port, client = _bus_read(stamp=False)
    port.read(ModelPointQuery(query="open-claims"))
    port.close()
    assert "deadline" not in client.requests[0]


def test_command_route_stamps_the_deadline_on_the_stdin_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["input"] = json.loads(kwargs["input"])
        answer = {
            "correlation_id": seen["input"]["correlation_id"],
            "query": "open-claims",
            "status": "ok",
            "matched": 0,
            "open_claims": [],
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer))

    monkeypatch.setattr(subprocess, "run", fake_run)
    CommandPointRead(
        ["onex", "ledger", "query", "--stdin"],
        WAIT_S,
        stamp_deadline=True,
        clock=_clock,
    ).read(ModelPointQuery(query="open-claims"))
    assert datetime.fromisoformat(seen["input"]["deadline"]) == SENT + timedelta(
        seconds=WAIT_S
    )


def test_stamp_deadline_is_a_declared_route_field_off_by_default() -> None:
    off = ModelBusSessionReadRoute(
        kind="bus-session", bus_lane="dev", timeout_seconds=WAIT_S
    )
    on = ModelBusSessionReadRoute(
        kind="bus-session", bus_lane="dev", timeout_seconds=WAIT_S, stamp_deadline=True
    )
    assert off.stamp_deadline is False
    assert on.stamp_deadline is True
    command = ModelCommandReadRoute(
        kind="command", command_env="X", timeout_seconds=WAIT_S, stamp_deadline=True
    )
    assert command.stamp_deadline is True


def test_build_point_read_carries_the_route_field_to_the_port() -> None:
    route = ModelBusSessionReadRoute(
        kind="bus-session", bus_lane="dev", timeout_seconds=WAIT_S, stamp_deadline=True
    )
    port = build_point_read(route)
    assert isinstance(port, BusSessionPointRead)
    assert port.stamps_deadline is True
