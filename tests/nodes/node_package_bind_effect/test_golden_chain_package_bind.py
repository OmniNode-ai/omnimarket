# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the package bind effect.

Publish the request on the command topic and assert the one event it produces, under the request's correlation id,
ending in the run-completed event; the last test shows the assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_package_bind_effect.handlers.handler_package_bind import (
    HandlerPackageBind,
    answer_command,
)
from omnimarket.nodes.node_package_bind_effect.models.model_package_bind import (
    ModelPackageBindRequest,
)
from tests.nodes.node_package_bind_effect.conftest import DEMO, PACKAGE

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_package_bind_effect/contract.yaml"
)
TOPICS: dict[str, str] = {
    "package_bind_requested": "onex.cmd.omnimarket.package-bind-requested.v1",
    "package_bind_run_completed": "onex.evt.omnimarket.package-bind-run-completed.v1",
    "package_bind_run_failed": "onex.evt.omnimarket.package-bind-run-failed.v1",
}
CORRELATION = "chain-1"
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.package-bind-run-completed.v1"


def request_payload(src: Path, node: str = DEMO) -> bytes:
    return (
        ModelPackageBindRequest(correlation_id=CORRELATION, src=str(src), node=node)
        .model_dump_json()
        .encode("utf-8")
    )


async def publish(payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    handler = HandlerPackageBind()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["package_bind_requested"],
            on_message=on_command,
            group_id="package-bind",
        )
        for role in ("package_bind_run_completed", "package_bind_run_failed"):
            await bus.subscribe(
                TOPICS[role],
                on_message=collector(TOPICS[role]),
                group_id=f"test-{role}",
            )
        await bus.publish(TOPICS["package_bind_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, Any]]], src: Path) -> None:
    assert [topic for topic, _ in events] == [TOPICS["package_bind_run_completed"]]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["ok"] is True
    assert body["package"] == PACKAGE
    assert body["path"] == str(src / "omnimarket" / "nodes" / DEMO)


def test_the_contract_names_the_topics_the_chain_uses() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    assert contract["topics"] == TOPICS
    assert contract["externally_consumed_topics"] == [
        TOPICS["package_bind_run_completed"],
        TOPICS["package_bind_run_failed"],
    ]


def test_golden_chain_a_request_ends_in_the_run_completed_event(
    demo_tree: Path,
) -> None:
    events = asyncio.run(publish(request_payload(demo_tree)))
    assert_golden_chain(events, demo_tree)


def test_golden_chain_turns_red_when_the_event_is_renamed(demo_tree: Path) -> None:
    """Named break: the answer goes out on the failed topic; the golden assertion must refuse it."""
    events = asyncio.run(publish(request_payload(demo_tree)))
    broken = [(TOPICS["package_bind_run_failed"], body) for _, body in events]
    with pytest.raises(AssertionError):
        assert_golden_chain(broken, demo_tree)
