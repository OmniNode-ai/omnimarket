# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host the drain verb stopped placement on takes no lane (OMN-24489).

The drain mark rides the placement request as ``draining_hosts``: a host named there is refused for
either engine, on every rung of the ladder, until it is undrained, and a pin to it names a draining
host and not a place to run. The handler no longer reads any drain state itself; a request that names
none is an old producer's and places as before.
"""

from __future__ import annotations

import pytest

from omnimarket.models.remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)


def _reading(name: str, rank_free: float) -> ModelRemoteLaneHostReading:
    return ModelRemoteLaneHostReading.model_validate(
        {
            "name": name,
            "engines": ("claude_sonnet", "codex"),
            "lane_slots": 2,
            "lane_cap": 4,
            "rank_free": rank_free,
        }
    )


def _two_readings() -> tuple[ModelRemoteLaneHostReading, ModelRemoteLaneHostReading]:
    # A ranks first: absent a drain mark it takes the lane (the pick before the change).
    return (_reading("A", 5.0), _reading("B", 1.0))


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
def test_a_draining_host_takes_no_lane_and_the_other_places(engine: str) -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            draining_hosts=("A",),
            readings=_two_readings(),
        )
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "B"
    reasons = {v.host: v.reason for v in result.verdicts}
    assert reasons["A"].startswith("draining")


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
def test_without_the_field_the_pick_is_as_before(engine: str) -> None:
    """Failing control: the same request without ``draining_hosts`` places A.

    On the code before the change the request below is the only one there is and it places A; the
    drained one could not even be built. The two picks differ, and the undrained pick is A.
    """
    legacy = ModelRemoteLanePlacementRequest(engine=engine, readings=_two_readings())
    drained = legacy.model_copy(update={"draining_hosts": ("A",)})
    assert HandlerRemoteLanePlacement().handle(legacy).host == "A"
    assert HandlerRemoteLanePlacement().handle(drained).host == "B"


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
def test_with_every_host_draining_the_outcome_is_no_host(engine: str) -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            draining_hosts=("A", "B"),
            readings=_two_readings(),
        )
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    reasons = {v.host: v.reason for v in result.verdicts}
    assert reasons["A"].startswith("draining")
    assert reasons["B"].startswith("draining")


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["claude_sonnet", "codex"])
def test_a_pin_to_a_draining_host_is_refused(engine: str) -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            pinned_host="A",
            draining_hosts=("A",),
            readings=_two_readings(),
        )
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    reasons = {v.host: v.reason for v in result.verdicts}
    assert reasons["A"].startswith("draining")
    assert reasons["B"] == "not-pinned"
