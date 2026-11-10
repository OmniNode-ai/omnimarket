# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The chain runner stops its bus when the bus fails to start (OMN-23195).

``run_world_chain`` opens the decision bus and closes it in a ``finally``. A start that
raises part-way (the broker unreachable, a subscription refused) must still reach the
close, or the half-started bus leaks. The control is the base runner, where the start sits
before the ``try`` and this test reads red.
"""

from __future__ import annotations

import pytest

from tests.chain import landing_chain
from tests.chain.landing_chain import (
    DecisionBus,
    InmemoryDecisionBus,
    ModelLandingChainOverlay,
    run_world_chain,
)

pytestmark = pytest.mark.unit

SCENARIO = "S07_fix_submitted_awaiting_ci"


class BrokenStartError(RuntimeError):
    """The injected start failure."""


class StartFailingBus(InmemoryDecisionBus):
    """An in-memory bus whose start raises after it opened, and which counts its stops."""

    def __init__(self, overlay: ModelLandingChainOverlay) -> None:
        super().__init__(overlay)
        self.stops = 0

    async def start(self) -> None:
        await self.bus.start()
        raise BrokenStartError("the bus failed to start")

    async def stop(self) -> None:
        self.stops += 1
        await super().stop()


async def test_bus_start_failure_stops_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = ModelLandingChainOverlay()
    buses: list[StartFailingBus] = []

    def make(chosen: ModelLandingChainOverlay) -> DecisionBus:
        bus = StartFailingBus(chosen)
        buses.append(bus)
        return bus

    monkeypatch.setattr(landing_chain, "make_bus", make)
    with pytest.raises(BrokenStartError):
        await run_world_chain(SCENARIO, overlay)
    assert [bus.stops for bus in buses] == [1]
