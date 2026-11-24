# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A work-ledger query whose caller has left is dropped, not answered (OMN-26433).

On 2026-11-23 the one consumer of the query topic answered every request in order, a request whose
caller had stopped waiting 100 minutes earlier exactly like a live one, and with one partition the
dead requests were worked one by one while live callers waited (lag 4253). The query node now drops
a request older than its caller's declared bound and publishes no answer for it.

The harness is the in-memory event bus: requests are published on the node's command topic, the
handler the contract declares answers through the runtime's typed dispatch callback, and every
answer is published on the node's terminal topic. The assertion reads that topic.
"""

from __future__ import annotations

import importlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
NOW = datetime(2026, 11, 23, 16, 0, 0, tzinfo=UTC)
LOGGER_NAME = "omnimarket.nodes.node_work_ledger_query_effect"


def _declared_handler() -> tuple[Any, Any]:
    """The handler class and event model the contract routes the command topic to."""
    contract = yaml.safe_load(CONTRACT.read_text())
    route = contract["handler_routing"]["handlers"][0]
    module = importlib.import_module(route["handler"]["module"])
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    event_model = manifest.contracts[0].handler_routing.handlers[0].event_model
    return getattr(module, route["handler"]["name"]), event_model


def _request(
    *, age_s: float | None, max_age_s: float | None = None
) -> tuple[UUID, dict[str, Any]]:
    correlation_id = uuid4()
    payload: dict[str, Any] = {
        "correlation_id": str(correlation_id),
        "query": "rows",
        "now": NOW.isoformat(),
    }
    if age_s is not None:
        payload["requested_at"] = (NOW - timedelta(seconds=age_s)).isoformat()
    if max_age_s is not None:
        payload["max_age_seconds"] = max_age_s
    return correlation_id, payload


async def _publish_and_collect(
    payloads: list[dict[str, Any]],
) -> tuple[set[str], Any]:
    """Publish each request on the command topic; return the answered correlation ids."""
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    contract = yaml.safe_load(CONTRACT.read_text())
    command_topic = contract["event_bus"]["subscribe_topics"][0]
    terminal_topic = contract["event_bus"]["publish_topics"][0]
    handler_cls, event_model = _declared_handler()
    reader = InMemoryWorkLedgerQueryReader()
    handler = handler_cls(reader=reader, now=lambda: NOW)
    callback = _make_dispatch_callback(handler, event_model=event_model)

    bus = EventBusInmemory()
    await bus.start()

    async def on_command(message: Any) -> None:
        payload = json.loads(message.value)
        dispatch = await callback(
            {"payload": payload, "correlation_id": payload["correlation_id"]}
        )
        if dispatch is None:
            return
        for event in dispatch.output_events:
            await bus.publish(
                terminal_topic, None, event.model_dump_json().encode("utf-8")
            )

    await bus.subscribe(
        command_topic, group_id="test-query-node", on_message=on_command
    )
    for payload in payloads:
        await bus.publish(command_topic, None, json.dumps(payload).encode("utf-8"))
    answers = await bus.get_event_history(topic=terminal_topic)
    return {json.loads(a.value)["correlation_id"] for a in answers}, handler


async def test_a_stale_request_gets_no_answer_and_a_live_request_does() -> None:
    stale_id, stale = _request(age_s=6000, max_age_s=60)
    live_id, live = _request(age_s=5, max_age_s=60)
    answered, handler = await _publish_and_collect([stale, live])
    assert str(live_id) in answered
    assert str(stale_id) not in answered
    assert handler.expired_dropped == 1


async def test_a_request_with_no_bound_of_its_own_takes_the_contract_default() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    default_s = contract["work_ledger_query"]["request_expiry"][
        "default_max_age_seconds"
    ]
    stale_id, stale = _request(age_s=default_s + 60)
    live_id, live = _request(age_s=default_s - 60)
    answered, _ = await _publish_and_collect([stale, live])
    assert str(live_id) in answered
    assert str(stale_id) not in answered


async def test_a_request_that_names_no_send_time_is_answered_as_before() -> None:
    old_id, old = _request(age_s=None)
    answered, _ = await _publish_and_collect([old])
    assert str(old_id) in answered


async def test_each_drop_is_counted_and_logged_once_with_its_age(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, first = _request(age_s=6000, max_age_s=60)
    _, second = _request(age_s=7200, max_age_s=60)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _, handler = await _publish_and_collect([first, second])
    drops = [r for r in caplog.records if "expired" in r.getMessage()]
    assert len(drops) == 2
    assert "age_s=6000" in drops[0].getMessage()
    assert "age_s=7200" in drops[1].getMessage()
    assert handler.expired_dropped == 2
