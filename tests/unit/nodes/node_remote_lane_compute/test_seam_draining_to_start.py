# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain verb's state rides the lane start to the placement request (OMN-24489).

The seam joins the start orchestrator's injected ``draining_hosts`` callable to
``HandlerRemoteLanePlacement`` (``node_remote_lane_compute``): the callable's answer rides the
placement request as ``draining_hosts`` and names the host the pick may not take, and a drain
state that cannot be read ends the start ``no-host`` naming ``drain-state-unreadable`` -- never
read as no host draining.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)

from omnimarket.models.remote_lane_placement import (
    ModelRemoteLanePlacementRequest,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EXIT_CODES,
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    request,
)

pytestmark = pytest.mark.unit


class RecordingPlacement(HandlerRemoteLanePlacement):
    """The real placement handler, remembering the request the orchestrator built."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[ModelRemoteLanePlacementRequest] = []

    def handle(self, request: ModelRemoteLanePlacementRequest) -> Any:
        self.seen.append(request)
        return super().handle(request)


def _pool() -> ModelLaneHostPoolSnapshot:
    """Two lab hosts; h106 ranks first, so absent a drain mark it takes the lane."""

    def reading(name: str, rank_free: float) -> ModelLaneHostPoolReading:
        return ModelLaneHostPoolReading(
            name=name,
            target=name,
            engines=("claude_opus", "claude_sonnet"),
            lane_slots=2,
            lane_cap=4,
            rank_free=rank_free,
            mem_avail_gb=16.0,
        )

    return ModelLaneHostPoolSnapshot(
        readings=(reading("h106", 4.0), reading("h200", 2.0))
    )


def test_a_draining_callable_never_places_on_the_draining_host(tmp_path: Path) -> None:
    recorder = RecordingPlacement()
    rig = Rig(
        tmp_path,
        pool=_pool(),
        placement=recorder,
        draining_hosts=lambda: ("h106",),
    )
    result = rig.orchestrator.run(request())
    assert result.host == "h200"
    assert result.receipt.host == "h200"
    [seen] = recorder.seen
    assert seen.draining_hosts == ("h106",)


def test_an_unreadable_drain_state_ends_the_start_no_host(tmp_path: Path) -> None:
    def broken() -> tuple[str, ...]:
        raise OSError("the drain journal cannot be read")

    rig = Rig(tmp_path, pool=_pool(), draining_hosts=broken)
    result = rig.orchestrator.run(request())
    assert result.host is None
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert result.exit_code == EXIT_CODES[EnumRemoteLaneStartOutcome.NO_HOST]
    assert result.receipt.state == EnumRemoteLaneStartOutcome.NO_HOST.value
    assert any(
        line.startswith("REMOTE-LANE-NO-HOST") and "drain-state-unreadable" in line
        for line in rig.printed
    )
