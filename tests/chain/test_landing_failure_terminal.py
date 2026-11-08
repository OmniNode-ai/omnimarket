# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A raised decision travels on the node's declared failure topic (OMN-23195).

The decision node's contract declares a success terminal and a failure terminal. The
chain harness must carry a decision that raised on the failure topic and a decision
that was taken on the success topic, and the controller side must receive each from
its own topic. The control is the base harness, which sent the raised decision on the
success topic.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_node,
)
from tests.chain.landing_chain import (
    DecisionNodeFailedError,
    ModelLandingChainOverlay,
    decision_topics,
    make_bus,
    run_world_chain,
)

pytestmark = pytest.mark.unit

SCENARIO = "S07_fix_submitted_awaiting_ci"


class DecisionRaisedError(RuntimeError):
    """The injected decision fault."""


async def test_raised_decision_is_carried_on_the_failure_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics = decision_topics()
    assert topics["success"] != topics["failure"]
    overlay = ModelLandingChainOverlay()
    buses = []

    def make(chosen: ModelLandingChainOverlay):  # type: ignore[no-untyped-def]
        bus = make_bus(chosen)
        buses.append(bus)
        return bus

    def raising(*_args: object) -> object:
        raise DecisionRaisedError("the decision node raised")

    from tests.chain import landing_chain

    monkeypatch.setattr(landing_chain, "make_bus", make)
    monkeypatch.setattr(decision_node, "decide_landing", raising)
    with pytest.raises(DecisionNodeFailedError, match="DecisionRaisedError"):
        await run_world_chain(SCENARIO, overlay)
    assert [bus.carried for bus in buses] == [[topics["command"], topics["failure"]]]


async def test_taken_decision_is_carried_on_the_success_topic() -> None:
    topics = decision_topics()
    _world, bus, records = await run_world_chain(SCENARIO, ModelLandingChainOverlay())
    assert bus.carried == [
        topic for _ in records for topic in (topics["command"], topics["success"])
    ]
