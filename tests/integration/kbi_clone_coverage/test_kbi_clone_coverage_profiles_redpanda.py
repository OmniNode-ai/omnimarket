# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The h101 layout, over the Redpanda overlay profile (OMN-22149).

The same body as ``tests/unit/kbi_clone_coverage/test_kbi_clone_coverage_profiles.py``, with the real
Kafka-backed bus of the declared test lane (the ``kafka_integration_bus`` fixture) in place of the
in-memory one.
"""

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from tests.helpers.kbi_clone_coverage_layout import (
    Layout,
    assert_node_covers_the_clone_outside_the_scan,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]


@pytest.mark.asyncio
async def test_kbi_clone_coverage_profiles_redpanda(
    kafka_integration_bus: EventBusKafka, layout: Layout
) -> None:
    await assert_node_covers_the_clone_outside_the_scan(kafka_integration_bus, layout)
