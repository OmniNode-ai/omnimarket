# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the host job claim read.

Inject a defined fault and assert the typed failure event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_host_job_claim_compute.handlers.handler_host_job_claim import (
    HandlerHostJobClaim,
    answer_command,
)
from tests.nodes.node_host_job_claim_compute.test_golden_chain_host_job_claim import (
    CORRELATION,
    TOPICS,
)

pytestmark = pytest.mark.unit


def _publish(payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    import asyncio as _asyncio

    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    handler = HandlerHostJobClaim()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = _asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    async def collect(topic: str, message: Any) -> None:
        seen.append((topic, json.loads(message.value)))
        if topic in (
            TOPICS["host_job_claim_run_completed"],
            TOPICS["host_job_claim_run_failed"],
        ):
            done.set()

    async def run() -> None:
        await bus.start()
        try:
            await bus.subscribe(
                TOPICS["host_job_claim_requested"],
                on_message=on_command,
                group_id="host-job-claim",
            )
            for topic in (
                TOPICS["host_job_claim_duplicate_found"],
                TOPICS["host_job_claim_run_completed"],
                TOPICS["host_job_claim_run_failed"],
            ):
                await bus.subscribe(
                    topic,
                    on_message=lambda m, t=topic: collect(t, m),
                    group_id=f"test-{topic}",
                )
            await bus.publish(
                TOPICS["host_job_claim_requested"], key=None, value=payload
            )
            await _asyncio.wait_for(done.wait(), timeout=5)
        finally:
            await bus.close()

    _asyncio.run(run())
    return seen


def test_error_chain_bad_payload_gives_one_failed_event() -> None:
    events = _publish(b'{"write": "maybe"}')
    assert [topic for topic, _ in events] == [TOPICS["host_job_claim_run_failed"]]
    assert events[0][1]["failure"].startswith("bad-payload:")


def test_error_chain_a_unit_claimed_by_two_jobs_gives_a_failed_event() -> None:
    payload = json.dumps(
        {
            "correlation_id": CORRELATION,
            "host": "host-a",
            "installed_units": [{"name": "onex-lane-reaper", "command": "/bin/x"}],
            "baseline": {
                "role": "lane-reaper",
                "jobs": [
                    {
                        "job_id": "job-a",
                        "owner_unit": "onex-lane-reaper",
                        "owner_command_marker": "marker",
                        "claimants": ["onex-lane-reaper", "onex-old-reaper"],
                    },
                    {
                        "job_id": "job-b",
                        "owner_unit": "onex-other",
                        "owner_command_marker": "other",
                        "claimants": ["onex-other", "onex-old-reaper"],
                    },
                ],
            },
        }
    ).encode()
    events = _publish(payload)
    assert [topic for topic, _ in events] == [TOPICS["host_job_claim_run_failed"]]
    assert events[0][1]["failure"] == "bad-payload:baseline"
