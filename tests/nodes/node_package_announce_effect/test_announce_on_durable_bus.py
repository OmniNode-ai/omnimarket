# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The same announce scenario on a real durable bus (OMN-21597, A1 acceptance: both overlays).

Only the overlay's bus binding differs from the in-memory runs. The topic and the
announcer are unique per run, so the topic holds no record from an earlier run, and a
second consumer group reads the topic independently to show the records reached the
broker and not only the local catalog.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)
from omnibase_core.protocols.services.protocol_announce_bus import ProtocolAnnounceBus

from omnimarket.nodes.node_package_announce_effect.announce_step0 import AnnounceStep0
from omnimarket.nodes.node_package_announce_effect.protocol_announce_message import (
    ProtocolAnnounceMessage,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    AnnounceTestBindings,
    make_overlay,
)
from tests.nodes.node_package_announce_effect.topics import DURABLE_TOPIC_PREFIX


@pytest.mark.integration
@pytest.mark.kafka
async def test_three_contracts_are_announced_and_cataloged_over_the_broker(
    kafka_integration_bus: ProtocolAnnounceBus[ProtocolAnnounceMessage],
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
) -> None:
    bindings.register_bus("durable", kafka_integration_bus)
    announcer: UUID = uuid4()
    topic = f"{DURABLE_TOPIC_PREFIX}{announcer.hex[:12]}.v1"
    overlay = make_overlay("durable", announcer_id=announcer).model_copy(
        update={
            "announcement_topic": topic,
            "acknowledgement_attempts": 150,
            "acknowledgement_poll_seconds": 0.2,
        }
    )
    seen: list[ModelNodeAnnouncement] = []

    async def observe(message: ProtocolAnnounceMessage) -> None:
        seen.append(ModelNodeAnnouncement.model_validate_json(message.value))

    stop_observing = await kafka_integration_bus.subscribe(
        topic, on_message=observe, group_id=f"observer.{announcer.hex[:12]}"
    )
    step0 = AnnounceStep0(overlay=overlay, bindings=bindings.registry)
    try:
        catalog = await step0.run()
        for _ in range(100):
            if {a.entity_id for a in seen} >= set(standard_contracts):
                break
            await asyncio.sleep(0.2)
    finally:
        await stop_observing()

    assert step0.report is not None
    assert set(step0.report.announced) == set(standard_contracts)
    assert step0.report.refused == {}
    snapshot = catalog.snapshot()
    assert set(snapshot) == set(standard_contracts)
    assert {e.state for e in snapshot.values()} == {EnumCatalogEntryState.ADMITTED}
    assert {a.entity_id for a in seen} == set(standard_contracts), (
        "falsifier: a record that never reached the broker"
    )
    for record in seen:
        assert record.content_hash == ModelNodeAnnouncement.canonical_content_hash(
            standard_contracts[record.entity_id]
        )
        assert record.announcer_id == announcer
