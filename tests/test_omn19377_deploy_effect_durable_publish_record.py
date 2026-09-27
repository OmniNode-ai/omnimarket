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
    _CountingBus,
    _deploy_agent,
    _DurableArms,
    _envelope,
)


@pytest.mark.unit
async def test_a_redelivery_to_a_new_process_is_not_published() -> None:
    """The recreate case: the first process never heard back, a fresh one gets the copy."""
    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, silent=True)
        first_process = HandlerDeployPublishMonitor(event_bus=bus)
        await first_process.handle(_envelope(_LIVE_CORRELATION))

        # A short wait, so a copy that does reach the agent fails in seconds.
        after_recreate = HandlerDeployPublishMonitor(event_bus=bus)
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
    """The deadline case: a DLQ replay lands while the first dispatch is still running.

    Since OMN-18143 the command arm returns at once, so this copy normally arrives
    after it; the test keeps the harder interleaving, a replay racing the first
    dispatch, so the record's guarantee does not rest on that timing.
    """
    bus = _CountingBus()
    await bus.start()
    abandoned: asyncio.Task[object] | None = None
    try:
        await _deploy_agent(bus, silent=True)
        first = HandlerDeployPublishMonitor(event_bus=bus)
        abandoned = asyncio.create_task(first.handle(_envelope(_LIVE_CORRELATION)))
        for _ in range(100):
            if bus.commands:
                break
            await asyncio.sleep(0.01)

        # A short wait on the replay, so that a replay which does reach the agent
        # fails this test in seconds instead of holding it for the production 600 s.
        replay_handler = HandlerDeployPublishMonitor(event_bus=bus)
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
        arms = await _DurableArms(bus).start()
        await _deploy_agent(bus, first_answer=EnumDeployRejectionReason.BUSY)
        first = HandlerDeployPublishMonitor(event_bus=bus)
        await first.handle(_envelope(_LIVE_CORRELATION))
        await arms.wait_for(1)
        second = HandlerDeployPublishMonitor(event_bus=bus)
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
        handler = HandlerDeployPublishMonitor(event_bus=bus)
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


@pytest.mark.unit
async def test_a_busy_seen_only_by_the_rejection_arm_releases_the_record() -> None:
    """The command arm does not wait (OMN-18143), so the durable rejection arm releases it."""
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        TOPIC_REBUILD_REJECTED,
    )

    bus = _CountingBus()
    await bus.start()
    try:
        await _deploy_agent(bus, silent=True)
        gone = HandlerDeployPublishMonitor(event_bus=bus)
        await gone.handle(_envelope(_LIVE_CORRELATION))

        arm = HandlerDeployPublishMonitor(event_bus=bus)
        await arm.handle(
            ModelEventEnvelope[object](
                payload={
                    "correlation_id": _LIVE_CORRELATION,
                    "reason": EnumDeployRejectionReason.BUSY.value,
                    "scope": "full",
                },
                event_type=TOPIC_REBUILD_REJECTED,
            )
        )

        later = HandlerDeployPublishMonitor(event_bus=bus)
        await later.handle(_envelope(_LIVE_CORRELATION))
    finally:
        await bus.close()

    assert bus.commands == [_LIVE_CORRELATION, _LIVE_CORRELATION]


@pytest.mark.unit
def test_a_busy_that_overtakes_the_record_leaves_no_record(tmp_path: object) -> None:
    """A busy recorded after the publish started may be that publish's answer.

    The rejection arm can see the agent's ``busy`` before the publishing handler
    resumes from its publish. The late record write is then skipped, which fails open
    to one more publish rather than marking a correlation the agent holds no job for.
    """
    from datetime import UTC, datetime, timedelta
    from pathlib import Path
    from uuid import UUID

    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.deploy_publish_record import (
        DeployPublishRecord,
    )

    record = DeployPublishRecord(Path(str(tmp_path)) / "record.json")
    correlation = UUID(_LIVE_CORRELATION)
    publish_started_at = datetime.now(UTC) - timedelta(seconds=1)

    record.release_busy(correlation)
    record.record(
        correlation,
        runtime_lane="dev",
        git_ref=None,
        rollback_target="previous",
        smoke_test=False,
        publish_started_at=publish_started_at,
    )
    assert not record.contains(correlation)

    record.record(
        correlation,
        runtime_lane="dev",
        git_ref=None,
        rollback_target="previous",
        smoke_test=False,
        publish_started_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert record.contains(correlation)


@pytest.mark.unit
def test_an_unreadable_record_fails_open(tmp_path: object) -> None:
    """A malformed file reads as empty: one more publish, never a dropped deploy."""
    from pathlib import Path
    from uuid import UUID

    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.deploy_publish_record import (
        DeployPublishRecord,
    )

    path = Path(str(tmp_path)) / "record.json"
    path.write_text("{not json", encoding="utf-8")
    assert not DeployPublishRecord(path).contains(UUID(_LIVE_CORRELATION))
