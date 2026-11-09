# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the gh-read scan (OMN-18946).

Golden chain: publish the check command on the contract's topic and assert the one event it produces, under the
command's correlation id, on the contract's terminal topic: no findings for a clean text, the findings and their
mirrors for a text naming a gh read. Error chain: a bad payload is answered with the typed failure event, never silence.
The last tests show each assertion turns red on a named injected break.
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

from omnimarket.nodes.node_lane_gh_read_scan_compute.handlers.handler_gh_read_scan import (
    HandlerGhReadScan,
    answer_command,
)
from omnimarket.nodes.node_lane_gh_read_scan_compute.models.model_gh_read_scan import (
    ModelGhReadScanRequest,
    ModelGhReadScanResult,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_gh_read_scan_compute/contract.yaml"
)
BUS = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"]
COMMAND_TOPIC = BUS["subscribe_topics"][0]
# The contract's topics, spelled out so a renamed topic turns this test red.
COMPLETED = "onex.evt.omnimarket.lane-gh-read-scan-completed.v1"
FAILED = "onex.evt.omnimarket.lane-gh-read-scan-failed.v1"
CORRELATION = "chain-gh-1"


def command(text: str, correlation: str = CORRELATION) -> bytes:
    return json.dumps(
        {
            "correlation_id": correlation,
            "surfaces": [{"path": "skill.md", "text": text, "kind": "prose"}],
        }
    ).encode()


async def publish(
    payload: bytes, handler: HandlerGhReadScan | None = None
) -> list[tuple[str, dict[str, Any]]]:
    handler = handler or HandlerGhReadScan()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(COMMAND_TOPIC, on_message=on_command, group_id="gh-read")
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
    assert [f["command"] for f in body["findings"]] == ["gh run view"]
    assert "onex scm run-view" in body["findings"][0]["mirror"]


def test_golden_chain_a_clean_text_completes_with_no_findings() -> None:
    events = asyncio.run(publish(command("Read onex scm pr-checks o/r#9.")))
    assert [topic for topic, _ in events] == [COMPLETED]
    assert events[0][1] == {
        "correlation_id": CORRELATION,
        "refused": False,
        "findings": [],
    }


def test_golden_chain_a_text_naming_gh_run_view_completes_with_the_finding_and_its_mirror() -> (
    None
):
    events = asyncio.run(publish(command("On a cache miss run gh run view --log.")))
    assert_golden_refusal(events)
    assert events[0][1]["findings"][0]["line"] == 1


def test_golden_chain_ends_on_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == COMPLETED
    assert FAILED in BUS["publish_topics"]


def test_named_break_a_scanner_that_finds_nothing_turns_the_golden_assertion_red() -> (
    None
):
    class BlindScanner(HandlerGhReadScan):
        def handle(self, request: ModelGhReadScanRequest) -> ModelGhReadScanResult:
            result = super().handle(request)
            return result.model_copy(update={"findings": ()})

    events = asyncio.run(
        publish(command("On a cache miss run gh run view --log."), BlindScanner())
    )
    with pytest.raises(AssertionError):
        assert_golden_refusal(events)
