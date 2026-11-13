# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain event test of the host job claim read.

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the run-completed event; the last test shows the assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_job_claim_compute.handlers.handler_host_job_claim import (
    answer_command,
)
from omnimarket.nodes.node_host_job_claim_compute.models import (
    ModelHostJob,
    ModelHostJobClaimRequest,
    ModelHostRoleBaseline,
    ModelInstalledUnit,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_job_claim_compute/contract.yaml"
)
TOPICS: dict[str, str] = {
    "host_job_claim_requested": "onex.cmd.omnimarket.host-job-claim-requested.v1",
    "host_job_claim_duplicate_found": "onex.evt.omnimarket.host-job-claim-duplicate-found.v1",
    "host_job_claim_run_completed": "onex.evt.omnimarket.host-job-claim-run-completed.v1",
    "host_job_claim_run_failed": "onex.evt.omnimarket.host-job-claim-run-failed.v1",
}
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "host_job_claim_duplicate_found",
        "host_job_claim_run_completed",
        "host_job_claim_run_failed",
    )
]
CORRELATION = "chain-1"
# The contract's terminal event, spelled out so a renamed topic turns this test red.
TERMINAL_EVENT = "onex.evt.omnimarket.host-job-claim-run-completed.v1"


def _request_payload(duplicate: bool) -> bytes:
    claimants = (
        ("onex-lane-reaper", "onex-old-reaper") if duplicate else ("onex-lane-reaper",)
    )
    installed = (
        ModelInstalledUnit(name="onex-lane-reaper", command="/bin/lane_reaper_node"),
    )
    if duplicate:
        installed = (
            *installed,
            ModelInstalledUnit(name="onex-old-reaper", command="/bin/old"),
        )
    request = ModelHostJobClaimRequest(
        correlation_id=CORRELATION,
        host="host-a",
        installed_units=installed,
        baseline=ModelHostRoleBaseline(
            role="lane-reaper",
            jobs=(
                ModelHostJob(
                    job_id="reap-lane",
                    owner_unit="onex-lane-reaper",
                    owner_command_marker="lane_reaper_node",
                    claimants=claimants,
                ),
            ),
        ),
    )
    return request.model_dump_json().encode("utf-8")


PAYLOAD = _request_payload(duplicate=False)
PAYLOAD_DUPLICATE = _request_payload(duplicate=True)


async def publish(payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    from omnimarket.nodes.node_host_job_claim_compute.handlers.handler_host_job_claim import (
        HandlerHostJobClaim,
    )

    handler = HandlerHostJobClaim()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["host_job_claim_run_completed"],
                TOPICS["host_job_claim_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["host_job_claim_requested"],
            on_message=on_command,
            group_id="host-job-claim",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["host_job_claim_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, Any]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_job_claim_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["ok"] is True
    assert events[0][1]["findings"] == 0
    assert events[0][1]["failed"] == 0


def test_golden_chain_no_duplicate_is_one_completed_event() -> None:
    events = asyncio.run(publish(PAYLOAD))
    assert_golden_chain(events)


def test_golden_chain_duplicate_gives_duplicate_then_completed() -> None:
    events = asyncio.run(publish(PAYLOAD_DUPLICATE))
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_job_claim_duplicate_found"],
        TOPICS["host_job_claim_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["findings"] == [
        {
            "kind": "second_unit",
            "job_id": "reap-lane",
            "owner_unit": "onex-lane-reaper",
            "extra_unit": "onex-old-reaper",
        }
    ]
    assert events[1][1]["ok"] is False
    assert events[1][1]["findings"] == 1
    assert events[1][1]["failed"] == 1


def test_named_break_turns_the_golden_assertion_red() -> None:
    """The golden assertion is not vacuous: with a duplicate it fails."""
    events = asyncio.run(publish(PAYLOAD_DUPLICATE))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
