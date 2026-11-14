# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the landing decision venv effect.

Publish the request on the command topic and assert the one event it produces, under the request's correlation id, ending in
the run-completed event; a second run reads ready; the last test shows the assertion turns red on a named break.
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

from omnimarket.nodes.node_landing_decision_venv_effect.handlers.handler_landing_decision_venv import (
    HandlerLandingDecisionVenv,
    answer_command,
)
from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelLandingDecisionVenvRequest,
)
from tests.nodes.node_landing_decision_venv_effect.conftest import (
    CORRELATION,
    PIN,
    FakeVenvRunner,
    make_overlay,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_landing_decision_venv_effect/contract.yaml"
)
TOPICS: dict[str, str] = {
    "landing_decision_venv_requested": "onex.cmd.omnimarket.landing-decision-venv-requested.v1",
    "landing_decision_venv_run_completed": "onex.evt.omnimarket.landing-decision-venv-run-completed.v1",
    "landing_decision_venv_run_failed": "onex.evt.omnimarket.landing-decision-venv-run-failed.v1",
}
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.landing-decision-venv-run-completed.v1"


def request_payload(request: ModelLandingDecisionVenvRequest) -> bytes:
    return request.model_dump_json().encode("utf-8")


async def publish(
    payload: bytes, handler: HandlerLandingDecisionVenv
) -> list[tuple[str, dict[str, Any]]]:
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
            TOPICS["landing_decision_venv_requested"],
            on_message=on_command,
            group_id="landing-decision-venv",
        )
        for role in (
            "landing_decision_venv_run_completed",
            "landing_decision_venv_run_failed",
        ):
            await bus.subscribe(
                TOPICS[role],
                on_message=collector(TOPICS[role]),
                group_id=f"test-{role}",
            )
        await bus.publish(
            TOPICS["landing_decision_venv_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, Any]]], outcome: str) -> None:
    assert [topic for topic, _ in events] == [
        TOPICS["landing_decision_venv_run_completed"]
    ]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["ok"] is True
    assert body["outcome"] == outcome
    assert body["versions"] == {
        "pydantic": PIN["pydantic"],
        "pydantic-core": PIN["pydantic_core"],
        "pyyaml": PIN["pyyaml"],
    }


def test_the_contract_names_the_topics_the_chain_uses() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT
    assert contract["topics"] == TOPICS
    assert contract["externally_consumed_topics"] == [
        TOPICS["landing_decision_venv_run_completed"],
        TOPICS["landing_decision_venv_run_failed"],
    ]


def test_golden_chain_a_request_installs_every_pinned_dependency_then_reads_ready(
    make_request: Callable[..., ModelLandingDecisionVenvRequest],
) -> None:
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner()
    )
    payload = request_payload(make_request())
    first = asyncio.run(publish(payload, handler))
    assert_golden_chain(first, "installed")
    second = asyncio.run(publish(payload, handler))
    assert_golden_chain(second, "ready")


def test_golden_chain_turns_red_when_the_event_is_renamed(
    make_request: Callable[..., ModelLandingDecisionVenvRequest],
) -> None:
    """Named break: the answer goes out on the failed topic; the golden assertion must refuse it."""
    handler = HandlerLandingDecisionVenv(
        overlay=make_overlay(), runner=FakeVenvRunner()
    )
    events = asyncio.run(publish(request_payload(make_request()), handler))
    broken = [(TOPICS["landing_decision_venv_run_failed"], body) for _, body in events]
    with pytest.raises(AssertionError):
        assert_golden_chain(broken, "installed")
