# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SEAM tests of the declared-clone tick, over the in-memory profile (OMN-23557).

The Redpanda overlay profile runs the same body in
``tests/integration/declared_clone_tick/test_declared_clone_tick_seam_redpanda.py``.
"""

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from tests.helpers.declared_clone_tick_seam import (
    assert_overlay_and_unit_seams,
    assert_tick_seam,
)
from tests.helpers.kbi_clone_coverage_layout import Layout

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_declared_clone_tick_seam_inmemory(layout: Layout) -> None:
    bus = EventBusInmemory(environment="local", group="declared-clone-tick-test")
    await bus.start()
    try:
        await assert_tick_seam(bus, layout)
    finally:
        await bus.close()


def test_declared_clone_tick_overlay_and_unit_seams() -> None:
    assert_overlay_and_unit_seams()


@pytest.mark.asyncio
async def test_declared_clone_tick_seam_control_fails_on_renamed_topic(
    layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.helpers.declared_clone_tick_seam as seam
    from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
        ModelDeclaredCloneTopics,
    )

    topics = seam.load_declared_clone_topics()
    monkeypatch.setattr(
        seam,
        "load_declared_clone_topics",
        lambda: ModelDeclaredCloneTopics(
            request=topics.request,
            receipt=topics.receipt + ".renamed",
            repo_role=topics.repo_role,
        ),
    )
    bus = EventBusInmemory(environment="local", group="declared-clone-tick-control")
    await bus.start()
    try:
        with pytest.raises(AssertionError):
            await assert_tick_seam(bus, layout)
    finally:
        await bus.close()
