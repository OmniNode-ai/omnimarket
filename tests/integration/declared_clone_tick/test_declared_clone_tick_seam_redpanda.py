# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SEAM tests of the declared-clone tick, over the Redpanda overlay profile (OMN-23557).

The same body as ``tests/unit/declared_clone_tick/test_declared_clone_tick_seam.py``, with the real
Kafka-backed bus of the declared test lane (the ``kafka_integration_bus`` fixture) in place of the
in-memory one.
"""

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from tests.helpers.declared_clone_tick_seam import assert_tick_seam
from tests.helpers.kbi_clone_coverage_layout import Layout

pytestmark = [pytest.mark.integration, pytest.mark.kafka]


@pytest.mark.asyncio
async def test_declared_clone_tick_seam_redpanda(
    kafka_integration_bus: EventBusKafka, layout: Layout
) -> None:
    await assert_tick_seam(kafka_integration_bus, layout)
