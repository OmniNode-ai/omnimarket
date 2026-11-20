# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The item owner workflow on Temporal's time-skipping server (OMN-25981): one owner step per key, whatever asks."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from omnimarket.nodes.node_item_owner_dispatch_effect.handlers.temporal_item_owner import (
    TICK_WORKFLOW_NAME,
    ItemOwnerPlaceWorkflow,
    ItemOwnerTickWorkflow,
    ModelItemOwnerSteps,
    ModelItemOwnerTickPlan,
    ModelItemOwnerTickResult,
    item_owner_step,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerOverlay,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.runtime_item_owner_worker import (
    build_schedule,
)

pytestmark = pytest.mark.unit

FAKE = Path(__file__).with_name("fake_item_owner_node.py")
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_item_owner_dispatch_effect"
)


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        yield environment


def _plan() -> ModelItemOwnerTickPlan:
    fake = (sys.executable, str(FAKE))
    return ModelItemOwnerTickPlan(
        steps=ModelItemOwnerSteps(
            plan=(*fake, "plan"), place=(*fake, "place"), read=(*fake, "read")
        ),
        child_prefix="item-owner-",
        step_timeout_seconds=60,
        step_max_attempts=1,
    )


async def _tick(client: Client, queue: str) -> ModelItemOwnerTickResult:
    result = await client.execute_workflow(
        TICK_WORKFLOW_NAME,
        _plan().model_dump_json(),
        id=f"tick-{uuid.uuid4()}",
        task_queue=queue,
    )
    return ModelItemOwnerTickResult.model_validate_json(str(result))


async def test_one_owner_per_item_across_ticks_and_reruns(
    env: WorkflowEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "steps.log"
    monkeypatch.setenv("FAKE_ITEM_OWNER_LOG", str(log))
    queue = f"q-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ItemOwnerTickWorkflow, ItemOwnerPlaceWorkflow],
        activities=[item_owner_step],
    ):
        first = await _tick(env.client, queue)
        second = await _tick(env.client, queue)
        rerun = await _tick(env.client, queue)
    steps = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("place ") for line in steps) == 1
    assert [p.lane for p in first.placed] == ["own-0123456789-g1"]
    assert first.reading_exit == 1
    assert second.placed == ()
    assert second.already_started == ("item-owner-itm-0123456789-rabcdefabcdef-g1",)
    assert rerun.placed == ()


async def test_one_owner_per_item_when_two_ticks_start_together(
    env: WorkflowEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "steps.log"
    monkeypatch.setenv("FAKE_ITEM_OWNER_LOG", str(log))
    monkeypatch.setenv("FAKE_ITEM_OWNER_KEY", f"-{uuid.uuid4().hex[:8]}")
    queue = f"q-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ItemOwnerTickWorkflow, ItemOwnerPlaceWorkflow],
        activities=[item_owner_step],
    ):
        racing = await asyncio.gather(
            _tick(env.client, queue), _tick(env.client, queue)
        )
    steps = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("place ") for line in steps) == 1
    assert sorted(len(r.placed) for r in racing) == [0, 1]
    assert sorted(len(r.already_started) for r in racing) == [0, 1]


def test_the_schedule_takes_every_bound_from_the_overlay() -> None:
    overlay = ModelItemOwnerOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )
    schedule = build_schedule(overlay.temporal, "/registry")
    assert (
        schedule.spec.intervals[0].every.total_seconds()
        == overlay.temporal.interval_minutes * 60
    )
    assert schedule.action.task_queue == overlay.temporal.task_queue
    assert schedule.action.id == overlay.temporal.schedule_id
    plan = ModelItemOwnerTickPlan.model_validate_json(str(schedule.action.args[0]))
    assert plan.steps.plan[0] == sys.executable
    assert "/registry" in plan.steps.plan
