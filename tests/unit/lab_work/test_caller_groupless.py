# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21764 -- the lab-work caller joins no consumer group.

Every lab-run call used to build a fresh ``lab_work_client`` group (three
subscriptions in it) that the broker kept for seven days: about 15,400 Empty
groups on the CI bus, whose offsets log the broker could not replay inside its
memory budget. The caller now reads with no group; the only group a lab-work
exchange creates is the serving host's own, which is the same for every call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work.bus import (
    LabWorkCaller,
    LabWorkHost,
    host_group_id,
    load_lab_work_topics,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    HandlerLabWorkUnitEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import HostReading

pytestmark = pytest.mark.unit

HOST = "h202"
SHA = "4c3985a451847571e2cebd06a6edf1940069946e"
CALLS = 25
#: Groups a lab-work exchange may create in total: the serving host's one stable group.
MAX_GROUPS = 1


class _RecordingBus(EventBusInmemory):
    """The in-memory bus, noting every consumer group and ungrouped read opened."""

    def __init__(self) -> None:
        super().__init__(environment="local", group="lab-work-groupless-test")
        self.groups: set[str] = set()
        self.ungrouped_topics: list[str] = []

    async def subscribe(
        self, *args: Any, **kwargs: Any
    ) -> Callable[[], Awaitable[None]]:
        group_id = kwargs.get("group_id")
        if group_id is not None and not group_id.startswith("ungrouped."):
            self.groups.add(group_id)
        return await super().subscribe(*args, **kwargs)

    async def subscribe_ungrouped(
        self, topic: str, on_message: Any
    ) -> Callable[[], Awaitable[None]]:
        self.ungrouped_topics.append(topic)
        return await super().subscribe_ungrouped(topic, on_message)


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8, load1=1.0, mem_available_bytes=32 * 1024**3, tools=("git", "uv")
        )


class _Runner:
    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=0,
            log_path=f"/lab-run/logs/{request.work_unit_id}.log",
            output_tail="42 passed",
            duration_seconds=1.0,
        )


def _request(index: int) -> ModelLabWorkUnitRequest:
    return ModelLabWorkUnitRequest.model_validate(
        {
            "work_unit_id": f"lw-{index:016x}",
            "target_host": HOST,
            "repo": "OmniNode-ai/omnimarket",
            "commit_sha": SHA,
            "argv": ["uv", "run", "pytest", "tests/unit", "-q"],
            "kind": "test",
            "timeout_seconds": 60,
            "lane": "groupless-test",
        }
    )


def test_the_contract_declares_that_the_caller_reads_with_no_group() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_lab_work_unit_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    assert contract["caller_subscription"] == {"mode": "ungrouped"}
    assert load_lab_work_topics().caller_subscription_mode == "ungrouped"


def test_many_calls_create_at_most_the_serving_hosts_one_group() -> None:
    async def scenario() -> tuple[_RecordingBus, list[ModelLabWorkUnitReceipt]]:
        bus = _RecordingBus()
        await bus.start()
        host = LabWorkHost(
            bus,
            HandlerLabWorkUnitEffect(HOST, _Runner()),
            HandlerHostCapacityAdvertiseEffect(_Reader()),
        )
        await host.start()
        receipts: list[ModelLabWorkUnitReceipt] = []
        # One caller per lab-run call, as `onex lab-work run` builds them.
        for index in range(CALLS):
            caller = LabWorkCaller(bus, wait_slack_seconds=5)
            await caller.start()
            receipts.append(await caller.dispatch(_request(index)))
            await caller.stop()
        await host.stop()
        await bus.close()
        return bus, receipts

    bus, receipts = asyncio.run(scenario())
    assert [r.status for r in receipts] == [EnumLabWorkUnitStatus.COMPLETED] * CALLS
    assert len(bus.groups) <= MAX_GROUPS
    assert bus.groups == {host_group_id(HOST)}
    assert not any("lab_work_client" in group for group in bus.groups)
    # Each call read the capacity topic and both terminals, none of them in a group.
    topics = load_lab_work_topics()
    assert sorted(bus.ungrouped_topics) == sorted(
        [topics.capacity, topics.success, topics.failure] * CALLS
    )


def test_the_caller_source_mints_no_group_name() -> None:
    source = Path(__file__).resolve().parents[3] / "src/omnimarket/lab_work/bus.py"
    text = source.read_text(encoding="utf-8")
    caller_source = text[text.index("class LabWorkCaller") :]
    assert "group_id" not in caller_source
    assert "uuid4().hex" not in caller_source.split("async def dispatch")[0]


def test_a_bus_that_cannot_read_without_a_group_is_refused_at_construction() -> None:
    from omnibase_core.event_bus.event_bus_inmemory import (
        EventBusInmemory as CoreEventBusInmemory,
    )

    core_bus = CoreEventBusInmemory(environment="local", group="no-ungrouped")
    with pytest.raises(TypeError, match="subscribe_ungrouped"):
        LabWorkCaller(core_bus)
