# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the host local config drift read (OMN-24242).

Publish the command on an in-memory bus and assert every event the node answers
with, in order, under one correlation id, ending in the run-completed event; the
last test shows the assertion turns red on a named break.
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

from omnimarket.nodes.node_host_local_config_drift_compute.handlers.handler_host_local_config_drift import (
    HandlerHostLocalConfigDrift,
    answer_command,
)
from omnimarket.nodes.node_host_local_config_drift_compute.models.model_host_local_config_drift import (
    ModelLocalConfigDriftCommand,
)
from tests.nodes.node_host_local_config_drift_compute.support import (
    HOST,
    declaration,
    measured_matching,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_local_config_drift_compute/contract.yaml"
)
TOPICS: dict[str, str] = dict(
    yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["topics"]
)
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "host_local_config_drift_clean",
        "host_local_config_drift_found",
        "host_local_config_drift_failed",
        "host_local_config_drift_run_completed",
    )
]
CORRELATION = "chain-1"
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.host-local-config-drift-run-completed.v1"
_HANDLER = HandlerHostLocalConfigDrift()


def matching_payload() -> bytes:
    """The command bytes of a measurement that agrees with its declaration."""
    declared = declaration()
    return (
        ModelLocalConfigDriftCommand(
            correlation_id=CORRELATION,
            declaration=declared,
            measured=measured_matching(declared),
        )
        .model_dump_json()
        .encode()
    )


def drifting_payload() -> bytes:
    """The command bytes of an agreeing measurement plus the wildcard safe directory."""
    declared = declaration()
    measured = measured_matching(declared).model_copy(
        update={"git_safe_directories": (*declared.git_safe_directories, "*")}
    )
    return (
        ModelLocalConfigDriftCommand(
            correlation_id=CORRELATION,
            declaration=declared,
            measured=measured,
        )
        .model_dump_json()
        .encode()
    )


async def publish(
    payload: bytes, topics: dict[str, str]
) -> list[tuple[str, dict[str, Any]]]:
    """Run one command through the bus and return the events it produced, in order."""
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, _HANDLER, topics):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic == topics["host_local_config_drift_run_completed"]:
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["host_local_config_drift_requested"],
            on_message=on_command,
            group_id="host-local-config-drift",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["host_local_config_drift_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(
    events: list[tuple[str, dict[str, Any]]],
    *,
    first_topic: str,
    verdict: str,
    finding_count: int,
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        first_topic,
        TOPICS["host_local_config_drift_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["host"] == HOST
    assert events[0][1]["verdict"] == verdict
    assert len(events[0][1]["findings"]) == finding_count
    assert events[-1][1]["failed"] == 0
    assert events[-1][1]["findings"] == finding_count


def test_golden_chain_an_agreeing_measurement_publishes_clean() -> None:
    events = asyncio.run(publish(matching_payload(), TOPICS))

    assert_golden_chain(
        events,
        first_topic=TOPICS["host_local_config_drift_clean"],
        verdict="working",
        finding_count=0,
    )
    assert events[0][1]["findings"] == []


def test_golden_chain_a_drifting_measurement_publishes_found() -> None:
    events = asyncio.run(publish(drifting_payload(), TOPICS))

    assert_golden_chain(
        events,
        first_topic=TOPICS["host_local_config_drift_found"],
        verdict="not working",
        finding_count=1,
    )
    assert events[0][1]["findings"] == [
        {
            "kind": "undeclared_safe_directory",
            "subject": "*",
            "detail": "measured but not declared",
        }
    ]


def test_golden_chain_ends_in_the_contract_terminal_event() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == TERMINAL_EVENT

    events = asyncio.run(publish(matching_payload(), TOPICS))
    assert events[-1][0] == TERMINAL_EVENT


def test_named_break_turns_the_golden_assertion_red() -> None:
    """The golden assertion is not vacuous: with the clean topic renamed it fails."""
    renamed = dict(TOPICS)
    renamed["host_local_config_drift_clean"] = (
        "onex.evt.omnimarket.host-local-config-drift-clean-renamed.v1"
    )
    events = asyncio.run(publish(matching_payload(), renamed))

    with pytest.raises(AssertionError):
        assert_golden_chain(
            events,
            first_topic=TOPICS["host_local_config_drift_clean"],
            verdict="working",
            finding_count=0,
        )
