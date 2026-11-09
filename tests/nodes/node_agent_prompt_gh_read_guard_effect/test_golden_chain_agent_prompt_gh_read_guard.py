# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the agent-prompt gh-read guard (OMN-23307).

Golden chain: publish the check command on the contract's topic and assert the one event it produces, under the
command's correlation id, on the contract's terminal topic: no findings for a prompt that names the mirror, the
finding and its mirror for a prompt that names a gh read. Error chain: a bad payload is answered with the typed
failure event, never silence. The last tests show each assertion turns red on a named injected break.
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

from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.handlers.handler_agent_prompt_gh_read_guard import (
    HandlerAgentPromptGhReadGuard,
    answer_command,
)
from omnimarket.nodes.node_agent_prompt_gh_read_guard_effect.models import (
    ModelAgentPromptGhReadDecision,
    ModelAgentPromptGhReadRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_agent_prompt_gh_read_guard_effect/contract.yaml"
)
BUS = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"]
COMMAND_TOPIC = BUS["subscribe_topics"][0]
# The contract's topics, spelled out so a renamed topic turns this test red.
COMPLETED = "onex.evt.omnimarket.agent-prompt-gh-read-guard-completed.v1"
FAILED = "onex.evt.omnimarket.agent-prompt-gh-read-guard-failed.v1"
CORRELATION = "chain-agent-prompt-1"


def command(prompt: str, correlation: str = CORRELATION) -> bytes:
    return json.dumps(
        {
            "correlation_id": correlation,
            "tool_name": "Agent",
            "surfaces": [{"label": "Agent prompt", "text": prompt}],
        }
    ).encode()


async def publish(
    payload: bytes, handler: HandlerAgentPromptGhReadGuard | None = None
) -> list[tuple[str, dict[str, Any]]]:
    handler = handler or HandlerAgentPromptGhReadGuard()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, COMPLETED, FAILED):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            COMMAND_TOPIC, on_message=on_command, group_id="agent-prompt-gh-read"
        )
        for topic in (COMPLETED, FAILED):
            await bus.subscribe(topic, on_message=collector(topic), group_id=topic)
        await bus.publish(COMMAND_TOPIC, key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_refusal(events: list[tuple[str, dict[str, Any]]]) -> None:
    assert [topic for topic, _ in events] == [COMPLETED]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["refused"] is True
    assert body["refusal_class"] == "agent-prompt-gh-read"
    assert [f["command"] for f in body["findings"]] == ["gh run view"]
    assert "onex scm run-view" in body["findings"][0]["mirror"]


def test_agent_prompt_gh_read_golden_chain_a_mirror_prompt_completes_with_no_findings() -> (
    None
):
    events = asyncio.run(publish(command("Read onex scm pr-checks o/r#9.")))
    assert [topic for topic, _ in events] == [COMPLETED]
    assert events[0][1] == {
        "correlation_id": CORRELATION,
        "refused": False,
        "findings": [],
        "refusal_class": "",
    }


def test_agent_prompt_gh_read_golden_chain_a_gh_read_prompt_completes_with_the_finding_and_its_mirror() -> (
    None
):
    assert_golden_refusal(
        asyncio.run(publish(command("On a cache miss run gh run view --log.")))
    )


def test_agent_prompt_gh_read_golden_chain_ends_on_the_contract_terminal_event() -> (
    None
):
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == COMPLETED
    assert FAILED in BUS["publish_topics"]


def test_agent_prompt_gh_read_error_chain_a_bad_payload_is_answered_with_a_typed_failure() -> (
    None
):
    events = asyncio.run(
        publish(json.dumps({"correlation_id": "bad-1", "tool_name": ""}).encode())
    )
    assert [topic for topic, _ in events] == [FAILED]
    assert events[0][1]["correlation_id"] == "bad-1"
    assert events[0][1]["failure"] == "bad-payload"


def test_agent_prompt_gh_read_error_chain_an_unparseable_payload_is_answered_not_dropped() -> (
    None
):
    events = asyncio.run(publish(b"not json"))
    assert [topic for topic, _ in events] == [FAILED]
    assert events[0][1]["correlation_id"] == ""


def test_agent_prompt_gh_read_named_break_a_guard_that_admits_everything_turns_the_golden_red() -> (
    None
):
    class AdmitAll(HandlerAgentPromptGhReadGuard):
        def handle(
            self, request: ModelAgentPromptGhReadRequest
        ) -> ModelAgentPromptGhReadDecision:
            return super().handle(request).model_copy(update={"findings": ()})

    events = asyncio.run(
        publish(command("On a cache miss run gh run view --log."), AdmitAll())
    )
    with pytest.raises(AssertionError):
        assert_golden_refusal(events)


def test_agent_prompt_gh_read_named_break_a_misrouted_failure_turns_the_error_chain_red() -> (
    None
):
    events = answer_command(
        b"not json",
        HandlerAgentPromptGhReadGuard(),
        COMPLETED,
        "onex.evt.omnimarket.renamed.v1",
    )
    with pytest.raises(AssertionError):
        assert [topic for topic, _ in events] == [FAILED]
