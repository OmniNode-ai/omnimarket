# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing whole-flow replay of the simulated world (OMN-22922, OMN-23195).

This test replays a simulation; it does not cross a live GitHub, worker or merge
boundary. The ``LandingWorld`` fake sets the fake pull request's state, so the merge
the receipt names is that fake's state set to merged by the replayed action.

What the run does cross is the decision bus: one test head goes from open to merged
through the landing decision node (a red pull request, a dispatched worker, a pushed
fix, CI turning green, a merge pinned to the new head), and every tick's snapshot and
decision travel over the overlay's bus. The run writes a receipt (profile, bus run id,
topics, every tick's actions and recorded outcomes, final simulated pull request
state). The receipt must name the merge or the run fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.chain.landing_chain import (
    MUTATIONS,
    ModelLandingChainOverlay,
    run_world_chain,
    write_receipt,
)

pytestmark = pytest.mark.unit

SCENARIO = "S07_fix_submitted_awaiting_ci"


async def replay_whole_flow(
    overlay: ModelLandingChainOverlay, out: Path
) -> dict[str, object]:
    """Replay the simulated scenario over the bus and read the receipt it wrote."""
    world, bus, records = await run_world_chain(SCENARIO, overlay)
    path = write_receipt(
        overlay, out, scenario=SCENARIO, world=world, bus=bus, records=records
    )
    receipt: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["merged"] == ["acme/app#1"], receipt
    return receipt


async def test_simulated_whole_flow_replay_takes_a_test_head_from_open_to_merged_over_the_bus_and_records_its_receipt(
    overlay: ModelLandingChainOverlay, tmp_path: Path
) -> None:
    receipt = await replay_whole_flow(overlay, tmp_path)
    assert receipt["profile"] == overlay.profile
    assert receipt["final_pr_states"] == {"acme/app#1": "merged"}
    ticks = receipt["ticks"]
    assert isinstance(ticks, list)
    assert ticks[0]["actions"] == ["dispatch_worker acme/app#1 real_red"]
    assert ticks[1]["actions"] == ["merge acme/app#1 h2"]
    assert receipt["messages_carried"] == 2 * len(ticks)


async def test_control_simulated_whole_flow_replay_is_red_when_the_node_never_lands(
    overlay: ModelLandingChainOverlay, tmp_path: Path
) -> None:
    with MUTATIONS["merge_never_lands"](), pytest.raises(AssertionError):
        await replay_whole_flow(overlay, tmp_path)
    assert (await replay_whole_flow(overlay, tmp_path))["merged"] == ["acme/app#1"]
