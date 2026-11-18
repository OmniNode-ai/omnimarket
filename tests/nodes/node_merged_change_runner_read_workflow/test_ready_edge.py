# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ready-edge scheduling, window selection, and alarm delivery."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnimarket.models.lab_alarm_delivery import EnumDeliveryCondition
from omnimarket.models.model_merged_change_reading import ModelMcrChange
from omnimarket.nodes.node_merged_change_runner_read_workflow import runtime_runner_read
from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
    ModelMergeFacts,
)

pytestmark = pytest.mark.unit

FIRST_BOOT = datetime(2026, 11, 18, 1, tzinfo=UTC)
FIRST_BUILD = FIRST_BOOT - timedelta(minutes=4)
SECOND_BOOT = FIRST_BOOT + timedelta(minutes=45)
SECOND_BUILD = SECOND_BOOT - timedelta(minutes=2)
SECOND_READY = SECOND_BOOT + timedelta(minutes=1)
THIRD_BOOT = SECOND_READY + timedelta(minutes=30)
THIRD_BUILD = THIRD_BOOT - timedelta(minutes=3)
THIRD_READY = THIRD_BOOT + timedelta(minutes=1)
BOUND = timedelta(minutes=10)


def _monitor():
    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.handler_runner_read_ready_edge import (
        HandlerRunnerReadReadyEdge,
    )
    from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read_ready_edge import (
        ModelRunnerReadReadyRequest,
    )

    monitor = SimpleNamespace(
        start=AsyncMock(),
        completed_at=AsyncMock(return_value=None),
        deliver=AsyncMock(return_value=True),
    )
    handler = HandlerRunnerReadReadyEdge(
        start=monitor.start,
        completed_at=monitor.completed_at,
        deliver=monitor.deliver,
        idle_bound=BOUND,
    )

    async def handle(state, snapshot, now):
        return await handler.handle(
            ModelRunnerReadReadyRequest(state=state, snapshot=snapshot, now=now)
        )

    monitor.handle = handle
    return monitor


def _snapshot(started_at, built_at, ready):
    from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read_ready_edge import (
        ModelRuntimeReadySnapshot,
    )

    return ModelRuntimeReadySnapshot(
        started_at=started_at, built_at=built_at, ready=ready
    )


@pytest.mark.asyncio
async def test_one_read_per_ready_boot_and_next_boot_starts_another():
    monitor = _monitor()
    state = await monitor.handle(
        None, _snapshot(FIRST_BOOT, FIRST_BUILD, True), FIRST_BOOT
    )
    assert monitor.start.await_count == 0
    booting = _snapshot(SECOND_BOOT, SECOND_BUILD, False)
    state = await monitor.handle(state, booting, SECOND_BOOT)
    assert monitor.start.await_count == 0

    ready = booting.model_copy(update={"ready": True})
    state = await monitor.handle(state, ready, SECOND_READY)
    edge = monitor.start.await_args.args[0]
    assert (edge.started_at, edge.previous_build_at, edge.ready_at) == (
        SECOND_BOOT,
        FIRST_BUILD,
        SECOND_READY,
    )

    for sample in (ready, booting, ready):
        state = await monitor.handle(
            state, sample, SECOND_READY + timedelta(seconds=30)
        )
    assert monitor.start.await_count == 1

    await monitor.handle(state, _snapshot(THIRD_BOOT, THIRD_BUILD, True), THIRD_READY)
    assert monitor.start.await_count == 2
    edge = monitor.start.await_args.args[0]
    assert (edge.started_at, edge.previous_build_at, edge.ready_at) == (
        THIRD_BOOT,
        SECOND_BUILD,
        THIRD_READY,
    )


@pytest.mark.asyncio
async def test_no_ready_edge_starts_no_read_and_raises_no_idle_alarm():
    monitor = _monitor()
    state = await monitor.handle(
        None, _snapshot(FIRST_BOOT, FIRST_BUILD, True), FIRST_BOOT
    )
    state = await monitor.handle(
        state, _snapshot(SECOND_BOOT, SECOND_BUILD, False), SECOND_BOOT
    )
    await monitor.handle(state, None, SECOND_BOOT + 2 * BOUND)
    monitor.start.assert_not_awaited()
    monitor.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_alarm_fires_once_and_recovers():
    monitor = _monitor()
    state = await monitor.handle(
        None, _snapshot(FIRST_BOOT, FIRST_BUILD, True), FIRST_BOOT
    )
    ready = _snapshot(SECOND_BOOT, SECOND_BUILD, True)
    state = await monitor.handle(state, ready, SECOND_READY)
    state = await monitor.handle(
        state, ready, SECOND_READY + BOUND - timedelta(seconds=1)
    )
    monitor.deliver.assert_not_awaited()

    state = await monitor.handle(state, ready, SECOND_READY + BOUND)
    state = await monitor.handle(
        state, ready, SECOND_READY + BOUND + timedelta(seconds=30)
    )
    raised = monitor.deliver.await_args.args[0]
    assert monitor.deliver.await_count == 1
    assert raised.outcome is EnumDeliveryCondition.ALARM
    assert raised.alarms

    monitor.completed_at.return_value = SECOND_READY + BOUND + timedelta(seconds=45)
    state = await monitor.handle(
        state, ready, SECOND_READY + BOUND + timedelta(minutes=1)
    )
    await monitor.handle(state, ready, SECOND_READY + 2 * BOUND)
    recovered = monitor.deliver.await_args.args[0]
    assert monitor.deliver.await_count == 2
    assert recovered.condition == raised.condition
    assert recovered.outcome is EnumDeliveryCondition.OK
    assert recovered.recovery_detail


def test_merge_window_excludes_bounds_and_retains_unread_cause():
    since, until = FIRST_BUILD, SECOND_READY
    facts = {
        1: since - timedelta(seconds=1),
        2: since,
        3: since + timedelta(seconds=1),
        4: until,
        5: until + timedelta(seconds=1),
        6: None,
        7: None,
    }
    changes = [
        ModelMcrChange(
            repo="omnimarket",
            number=number,
            merge_sha=f"{number:040x}" if number != 7 else "",
        )
        for number in facts
    ]

    def merge_facts(repo, sha):
        assert repo == "omnimarket"
        return ModelMergeFacts(merged_at=facts[int(sha, 16)])

    selected = runtime_runner_read.select_window_lines(
        changes, since, until, merge_facts
    )
    assert [line.number for line in selected] == [3, 4, 6, 7]
    assert [line.merge_sha for line in selected[:2]] == [f"{3:040x}", f"{4:040x}"]
    assert all(line.unread_cause for line in selected[2:])


@pytest.mark.asyncio
async def test_windowed_workflow_publishes_failed_read_with_cause():
    import json
    from uuid import uuid4

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.handler_runner_reading_integration import (
        HandlerRunnerReadingIntegration,
    )
    from omnimarket.nodes.node_merged_change_runner_read_workflow.handlers.temporal_runner_read import (
        ACTIVITY_NAME,
        MergedChangeRunnerReadWorkflow,
    )
    from omnimarket.nodes.node_merged_change_runner_read_workflow.models.model_runner_read import (
        ModelRunnerIntegrationConfig,
        ModelRunnerIntegrationRequest,
        ModelRunnerReadPublishInput,
        ModelRunnerReadWindow,
        ModelRunnerSelectedLine,
    )

    known = ModelRunnerSelectedLine(
        repo="omnimarket",
        number=7,
        merge_sha="a" * 40,
        merge=ModelMergeFacts(title="Known merge", merged_at=SECOND_BOOT),
    )
    unknown = ModelRunnerSelectedLine(
        repo="omnimarket",
        number=9,
        unread_cause="merge SHA could not be resolved",
    )
    calls = []

    @activity.defn(name=ACTIVITY_NAME)
    async def step(argv: list[str], stdin: str) -> str:
        calls.append(argv)
        if argv[-1] == "--window":
            window = ModelRunnerReadWindow.model_validate_json(stdin)
            assert window.previous_build_at == FIRST_BUILD
            assert window.as_of >= SECOND_BOOT
            return json.dumps(
                [line.model_dump(mode="json") for line in (known, unknown)]
            )
        if "--change" in argv:
            assert argv[-1] == f"{known.key}={known.merge_sha}"
            raise RuntimeError("reader transport refused")
        assert argv[-1] == "publish"
        return stdin

    cfg = runtime_runner_read.contract_config()
    plan = runtime_runner_read.build_plan(
        cfg, previous_build_at=FIRST_BUILD
    ).model_copy(update={"step_max_attempts": 1})
    queue = f"windowed-read-{uuid4()}"
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(
            env.client,
            task_queue=queue,
            workflows=[MergedChangeRunnerReadWorkflow],
            activities=[step],
        ),
    ):
        raw = await env.client.execute_workflow(
            MergedChangeRunnerReadWorkflow.run,
            plan.model_dump_json(),
            id=queue,
            task_queue=queue,
        )
    payload = ModelRunnerReadPublishInput.model_validate_json(raw)
    assert payload.windowed
    assert [outcome.key for outcome in payload.outcomes] == [known.key, unknown.key]
    assert all(outcome.reading is None for outcome in payload.outcomes)
    assert [call[-1] for call in calls] == [
        "--window",
        f"{known.key}={known.merge_sha}",
        "publish",
    ]
    result = HandlerRunnerReadingIntegration().handle(
        ModelRunnerIntegrationRequest(
            as_of=payload.as_of,
            origin="test",
            outcomes=payload.outcomes,
            merge_facts=payload.merge_facts,
            config=ModelRunnerIntegrationConfig.model_validate(cfg["integration"]),
        )
    )
    first, second = result.batch.readings
    assert result.counts.unread == 2
    assert first.state == second.state == "unproven"
    assert first.merged_at == SECOND_BOOT
    assert "reader transport refused" in first.step
    assert unknown.unread_cause in second.step


@pytest.mark.asyncio
async def test_incremental_projection_retains_rows_outside_read_window():
    from omnimarket.handlers.floor_projection import InMemorySnapshotSource
    from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
    from omnimarket.models.integration_reading import (
        ModelIntegrationReading,
        ModelIntegrationReadingBatch,
    )
    from omnimarket.nodes.node_integration_projection_compute import (
        HandlerIntegrationProjection,
    )
    from omnimarket.nodes.node_integration_reading_publish_effect.runtime_integration_reading_publish import (
        retain_previous_readings,
    )

    def row(number, read_at, cause):
        return ModelIntegrationReading(
            repository="omnimarket",
            target="dev runtime",
            number=number,
            title=f"Merge {number}",
            merged_at=FIRST_BUILD,
            state="unproven",
            gap_class="needs_reading",
            gap=cause,
            step="Read the change",
            read_at=read_at,
            origin="test",
        )

    previous = ModelIntegrationReadingBatch(
        as_of=SECOND_BOOT,
        origin="test",
        readings=(row(1, SECOND_BOOT, "Keep this row"), row(2, SECOND_BOOT, "Old")),
    )
    projection = HandlerIntegrationProjection().handle(previous)
    topic = "test.integration"
    source = InMemorySnapshotSource(
        {
            topic: [
                ModelFloorProjectionEnvelope(
                    projection_id="integration",
                    as_of=projection.as_of,
                    producer="test",
                    payload=projection.model_dump(mode="json"),
                )
                .model_dump_json()
                .encode()
            ]
        }
    )
    new = ModelIntegrationReadingBatch(
        as_of=SECOND_READY,
        origin="ready edge",
        readings=(row(2, SECOND_READY, "Updated"),),
    )
    merged = await retain_previous_readings(
        new, source, topic=topic, projection_id="integration"
    )
    updated = HandlerIntegrationProjection().handle(merged)
    assert updated.rows["omnimarket#1"] == previous.readings[0]
    assert updated.rows["omnimarket#2"] == new.readings[0]


@pytest.mark.asyncio
async def test_schedule_declares_ready_watcher_without_a_full_read():
    from temporalio.client import ScheduleOverlapPolicy

    client = AsyncMock()
    cfg = runtime_runner_read.contract_config()
    await runtime_runner_read.upsert_schedule(client, cfg, "test-queue")
    schedule = client.create_schedule.await_args.args[1]
    assert schedule.action.workflow == "MergedChangeRunnerReadReadyEdge"
    assert schedule.action.args == [cfg["ready_edge"]["tick_timeout_seconds"]]
    assert (
        schedule.spec.intervals[0].every.total_seconds()
        == cfg["ready_edge"]["probe_interval_seconds"]
    )
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
