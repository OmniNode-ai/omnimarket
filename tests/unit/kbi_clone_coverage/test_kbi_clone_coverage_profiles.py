# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The h101 layout, over the in-memory profile (OMN-22149).

The Redpanda overlay profile runs the same body in
``tests/integration/kbi_clone_coverage/test_kbi_clone_coverage_profiles_redpanda.py``.
Fixture: ``tests/fixtures/kbi_clone_coverage/h101_layout.yaml``.
"""

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from tests.helpers.kbi_clone_coverage_layout import (
    Layout,
    assert_node_covers_the_clone_outside_the_scan,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_kbi_clone_coverage_profiles_inmemory(layout: Layout) -> None:
    bus = EventBusInmemory(environment="local", group="kbi-clone-coverage-test")
    await bus.start()
    try:
        await assert_node_covers_the_clone_outside_the_scan(bus, layout)
    finally:
        await bus.close()
