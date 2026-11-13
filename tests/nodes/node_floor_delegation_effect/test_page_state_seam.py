# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the scenario states and counts the tracker writes are the ones the delegation page draws (OMN-23969).

The tracker (node_floor_delegation_effect) and the page that draws it (node_factory_floor_publish_effect/page) share a
vocabulary no bus carries: the state words of a scenario and the count fields of the tiles. A state the page does not
name is drawn as an undefined chip; a count the page does not tile is a number nobody sees. The test reads both sides, so
it fails when either one changes shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationCounts,
    ScenarioState,
)

PAGE = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/delegation.html"
)
# Counts that are totals or pass/fail rows with their own card text, not a tile of a state.
NOT_STATE_TILES = {"scenarios_total", "scenarios_passing", "scenarios_failing"}


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_every_scenario_state_the_tracker_writes_is_named_by_the_page() -> None:
    declared = re.search(r"const SCN = \{(.*?)\};", _page(), re.DOTALL)
    assert declared, "the page declares no SCN state table"
    named = set(re.findall(r"(\w+): \[", declared.group(1)))
    assert named == set(get_args(ScenarioState))


def test_every_scenario_count_the_tracker_writes_has_a_tile_on_the_page() -> None:
    page = _page()
    counted = {
        name
        for name in ModelDelegationCounts.model_fields
        if name.startswith("scenarios_") and name not in NOT_STATE_TILES
    }
    tiled = set(re.findall(r"c\.(scenarios_\w+)", page))
    assert counted <= tiled


def test_control_the_page_names_stale_with_its_own_tile() -> None:
    """The failing control: dropping `stale` from either side of the seam makes the two tests above fail."""
    page = _page()
    assert 'stale: ["Stale"' in page
    assert '"scs:stale"' in page
