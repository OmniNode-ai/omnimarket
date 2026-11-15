# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24367 AC2 -- the capacity inside the announcement is the capacity advertisement, field for field.

Both are published by one ``LabWorkHost`` beat on the in-memory bus; the announcement's ``capacity`` is compared with
the advertisement published at the same beat, and the field set with the advertisement model's own fields, so a field
gained or lost on either side fails here.
"""

from __future__ import annotations

import pytest

from omnimarket.lab_work.bus import LabWorkHost, load_lab_work_topics
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers import (
    HandlerHostFactsReadEffect,
    HostAnnouncer,
    load_host_announce_topics,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models import (
    ModelHostFactsProbeRequest,
)
from omnimarket.nodes.node_host_introspection_compute.models import (
    ModelHostAnnouncement,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    HandlerHostCapacityAdvertiseEffect,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import HostReading
from tests.nodes.node_host_introspection_announce_effect.support import (
    FakeFactsReader,
    RecordingBus,
)

pytestmark = pytest.mark.unit

HOST = "h201"


class _Reader:
    def read(self, tools: list[str]) -> HostReading:
        return HostReading(
            cores=8,
            load1=2.5,
            mem_available_bytes=12 * 1024**3,
            tools=("git", "uv"),
            mem_total_bytes=16 * 1024**3,
            swap_in_kb_s=0.0,
            swap_out_kb_s=0.0,
        )


class _Work:
    host_name = HOST

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=HOST,
            repo=request.repo,
            commit_sha=request.commit_sha,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=0,
        )


@pytest.mark.asyncio
async def test_announced_capacity_equals_the_capacity_advertisement() -> None:
    bus = RecordingBus()
    await bus.start()
    capacity_topic = load_lab_work_topics().capacity
    announced_topic = load_host_announce_topics().announced
    announcer = HostAnnouncer(
        bus,
        ModelHostFactsProbeRequest(host_name=HOST),
        facts_handler=HandlerHostFactsReadEffect(FakeFactsReader()),
    )
    host = LabWorkHost(
        bus,
        _Work(),
        HandlerHostCapacityAdvertiseEffect(_Reader()),
        announcer=announcer,
    )
    try:
        await host.advertise_once()
    finally:
        await bus.close()
    (advertised,) = bus.on(capacity_topic)
    (announced,) = bus.on(announced_topic)
    assert announced["capacity"] == advertised
    assert set(advertised) == set(ModelHostCapacityAdvertisement.model_fields)
    assert set(announced["capacity"]) == set(
        ModelHostCapacityAdvertisement.model_fields
    )
    ModelHostAnnouncement.model_validate(announced)


@pytest.mark.asyncio
async def test_a_failing_announcement_never_stops_the_capacity_beat() -> None:
    bus = RecordingBus()
    await bus.start()
    bus.down = {load_host_announce_topics().announced}
    host = LabWorkHost(
        bus,
        _Work(),
        HandlerHostCapacityAdvertiseEffect(_Reader()),
        announcer=HostAnnouncer(
            bus,
            ModelHostFactsProbeRequest(host_name=HOST),
            facts_handler=HandlerHostFactsReadEffect(FakeFactsReader()),
        ),
    )
    try:
        await host.advertise_once()
        await host.advertise_once()
    finally:
        await bus.close()
    assert len(bus.on(load_lab_work_topics().capacity)) == 2
    assert bus.on(load_host_announce_topics().announced) == []
