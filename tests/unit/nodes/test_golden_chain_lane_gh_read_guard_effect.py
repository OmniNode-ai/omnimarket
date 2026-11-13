# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_lane_gh_read_guard_effect routes a snapshot request to a class handler.

The contract binds each handler_routing entry to a class resolvable from its module, so infra
auto-wiring can build it. The snapshot policy picks the snapshot only for a lane outside Actions.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_gh_read_guard_effect.models.model_runner_placement_snapshot_request import (
    ModelRunnerPlacementSnapshotRequest,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_gh_read_guard_effect/contract.yaml"
)


def _routes() -> list[dict]:
    return yaml.safe_load(CONTRACT.read_text())["handler_routing"]["handlers"]


def test_every_routed_handler_resolves_to_a_class() -> None:
    for route in _routes():
        module = importlib.import_module(route["handler"]["module"])
        assert isinstance(getattr(module, route["handler"]["name"]), type)


def test_snapshot_route_chooses_snapshot_for_a_lane_outside_actions() -> None:
    route = next(
        r for r in _routes() if r["operation"] == "runner_placement_snapshot_check"
    )
    module = importlib.import_module(route["handler"]["module"])
    handler = getattr(module, route["handler"]["name"])()
    decision = handler.handle(
        ModelRunnerPlacementSnapshotRequest(
            snapshot_supplied=True, github_actions=False, lane_markers=("ONEX_LANE",)
        )
    )
    assert decision.snapshot_only is True


def test_error_leg_actions_runner_never_chooses_snapshot() -> None:
    module = importlib.import_module(
        "omnimarket.nodes.node_lane_gh_read_guard_effect.handlers.handler_runner_placement_snapshot_policy"
    )
    decision = module.HandlerRunnerPlacementSnapshotPolicy().handle(
        ModelRunnerPlacementSnapshotRequest(
            snapshot_supplied=True, github_actions=True, lane_markers=("ONEX_LANE",)
        )
    )
    assert decision.snapshot_only is False
