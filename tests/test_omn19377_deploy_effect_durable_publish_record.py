# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect's record of what it published outlives the process (OMN-19377 AC5).

WHAT WAS MEASURED ON THE .201 DEV LANE, 2026-09-24T19:00Z TO 2026-09-25T06:37Z
    After omnimarket#2838 (in-process memory of answered correlations) went live,
    ``onex.cmd.deploy.rebuild-requested.v1`` still carried 93 records for 43
    correlations, offsets 52977 to 53069. The deploy agent accepted 23 jobs and
    rejected 72 records: 49 ``duplicate``, 18 ``superseded_by_running_build``, 5
    ``superseded``, and 0 ``busy``. The 50 extra copies had three causes, all in the
    effect's consumer path, none in the orchestrator:

    - 22 followed a ``node_dlq_replay_effect`` copy of the deploy-publish record. A real
      rebuild takes about 20 minutes, the effect waits up to 600 s, and the runtime's
      dispatch deadline is 600 s, so the first dispatch of every real command was
      quarantined to ``onex.dlq.omnibase-infra.intents.v1`` as
      ``dispatch_deadline_exceeded`` and replayed about 10 minutes later.
    - 21 were the same deploy-publish record redelivered after a runtime recreate:
      the rebuild the effect was waiting on recreated the effect's own container, so
      the uncommitted record went to a fresh process with an empty memory. Job
      27b70e68 was re-asked at 05:02:42Z while its own job was running.
    - 7 were the same record redelivered to the same process while its first wait
      was still running or had timed out, which the memory did not cover because it
      remembered only answered correlations.

WHAT THIS PINS
    A rebuild command whose publish reached the broker is recorded under
    ``ONEX_STATE_DIR``, which on every lane is a named volume that survives a
    container recreate. Any later delivery of that correlation, to any handler
    instance, is answered from the record without publishing. ``busy`` is the one
    answer that removes the record, because the agent keeps no job for it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from omnimarket.events.runtime_deployment import EnumDeployRejectionReason
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    HandlerDeployPublishMonitor,
)
from tests.test_omn19377_deploy_effect_skips_repeats import (
    _LIVE_CORRELATION,
    _PRODUCTION_TIMEOUT_S,
    _CountingBus,
    _deploy_agent,
    _envelope,
)


@pytest.mark.unit
async def test_a_redelivery_to_a_new_process_is_not_published() -> None:
    """The recreate case: the first process never heard back, a fresh one gets the copy."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, silent=True)
        first_process = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=0.2, poll_interval_s=0.05
        )
        await first_process.handle(_envelope(_LIVE_CORRELATION))

        # A short wait, so a copy that does reach the agent fails in seconds.
        after_recreate = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=0.5, poll_interval_s=0.05
        )
        started = time.monotonic()
        repeat = await after_recreate.handle(_envelope(_LIVE_CORRELATION))
        elapsed = time.monotonic() - started
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION], (
        f"the redelivered command reached the agent again: {len(bus.commands)} commands"
    )
    assert elapsed < 1.0, f"the redelivery held the effect for {elapsed:.2f}s"
    assert repeat.metrics["duplicate_skipped"] == 1.0


@pytest.mark.unit
async def test_a_dlq_replay_while_the_first_wait_is_still_running_is_not_published() -> (
    None
):
    """The deadline case: the first dispatch was abandoned, not cancelled, and still waits."""
    bus = _CountingBus()
    await bus.start()
    abandoned: asyncio.Task[object] | None = None
    try:
        await _deploy_agent(bus, silent=True)
        first = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        abandoned = asyncio.create_task(first.handle(_envelope(_LIVE_CORRELATION)))
        for _ in range(100):
            if bus.commands:
                break
            await asyncio.sleep(0.01)

        # A short wait on the replay, so that a replay which does reach the agent
        # fails this test in seconds instead of holding it for the production 600 s.
        replay_handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=0.5, poll_interval_s=0.05
        )
        started = time.monotonic()
        replayed = await replay_handler.handle(_envelope(_LIVE_CORRELATION))
        elapsed = time.monotonic() - started
    finally:
        if abandoned is not None:
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION]
    assert elapsed < 1.0, f"the replayed copy held the effect for {elapsed:.2f}s"
    assert replayed.metrics["duplicate_skipped"] == 1.0


@pytest.mark.unit
async def test_a_busy_refusal_releases_the_durable_record() -> None:
    """``busy`` keeps no job at the agent, so a later copy must still be published."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, first_answer=EnumDeployRejectionReason.BUSY)
        first = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await first.handle(_envelope(_LIVE_CORRELATION))
        second = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await second.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, _LIVE_CORRELATION]


@pytest.mark.unit
async def test_the_record_is_a_file_under_the_state_dir(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record lives where the lane's named volume is, not in the process."""
    from pathlib import Path

    state_dir = Path(str(tmp_path)) / "state"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus)
        handler = HandlerDeployPublishMonitor(
            event_bus=bus, timeout_s=_PRODUCTION_TIMEOUT_S, poll_interval_s=0.05
        )
        await handler.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    records = list(state_dir.rglob("*.json"))
    assert records, f"nothing was written under {state_dir}"
    assert any(_LIVE_CORRELATION in p.read_text() for p in records)


@pytest.mark.unit
def test_a_missing_state_dir_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """No silent in-memory fallback: a lane without ONEX_STATE_DIR is mis-wired."""
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="ONEX_STATE_DIR"):
        HandlerDeployPublishMonitor(event_bus=_CountingBus())
