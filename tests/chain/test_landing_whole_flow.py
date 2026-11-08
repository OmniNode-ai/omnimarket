# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing whole-flow run (OMN-22922).

One test head goes from open to merged through the landing decision node: the head is a
red pull request, the node dispatches a worker, the worker pushes a fix, CI turns green
and the node merges pinned to the new head. Every tick's snapshot and decision cross the
overlay's bus, and the run writes a receipt (profile, bus run id, topics, every tick's
actions and recorded outcomes, final pull request state). The receipt must name the
merge or the run fails.
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


async def whole_flow(overlay: ModelLandingChainOverlay, out: Path) -> dict[str, object]:
    world, bus, records = await run_world_chain(SCENARIO, overlay)
    path = write_receipt(
        overlay, out, scenario=SCENARIO, world=world, bus=bus, records=records
    )
    receipt: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["merged"] == ["acme/app#1"], receipt
    return receipt


async def test_whole_flow_takes_a_test_head_from_open_to_merged_and_records_its_receipt(
    overlay: ModelLandingChainOverlay, tmp_path: Path
) -> None:
    receipt = await whole_flow(overlay, tmp_path)
    assert receipt["profile"] == overlay.profile
    assert receipt["final_pr_states"] == {"acme/app#1": "merged"}
    ticks = receipt["ticks"]
    assert isinstance(ticks, list)
    assert ticks[0]["actions"] == ["dispatch_worker acme/app#1 real_red"]
    assert ticks[1]["actions"] == ["merge acme/app#1 h2"]
    assert receipt["messages_carried"] == 2 * len(ticks)


async def test_control_whole_flow_is_red_when_the_node_never_lands(
    overlay: ModelLandingChainOverlay, tmp_path: Path
) -> None:
    with MUTATIONS["merge_never_lands"](), pytest.raises(AssertionError):
        await whole_flow(overlay, tmp_path)
    assert (await whole_flow(overlay, tmp_path))["merged"] == ["acme/app#1"]
