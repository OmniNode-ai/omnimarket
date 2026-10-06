# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane-liveness wire models live in a shared package (OMN-20604).

Other nodes (the lab-job checker first) import them from
``omnimarket.models.liveness``, never from the compute node's private package.
The node keeps its old import path as a re-export, so its contract, which names
``node_lane_liveness_compute.models.model_lane_liveness``, is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import omnimarket.models.liveness as shared
import omnimarket.nodes.node_lane_liveness_compute as node_pkg
from omnimarket.nodes.node_lane_liveness_compute.models import (
    model_lane_liveness as node_models,
)

_NAMES = (
    "EnumEvidenceBasis",
    "EnumLaneVerdict",
    "EnumRelayState",
    "ModelLaneLivenessReport",
    "ModelLaneLivenessRequest",
    "ModelLaneObservation",
    "ModelLaneVerdict",
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.unit
@pytest.mark.parametrize("name", _NAMES)
def test_shared_package_defines_each_model(name: str) -> None:
    obj = getattr(shared, name)
    assert obj.__module__.startswith("omnimarket.models.liveness")


@pytest.mark.unit
@pytest.mark.parametrize("name", _NAMES)
def test_node_paths_reexport_the_same_object(name: str) -> None:
    assert getattr(node_models, name) is getattr(shared, name)
    assert getattr(node_pkg, name) is getattr(shared, name)


@pytest.mark.unit
def test_shared_all_is_exactly_the_wire_models() -> None:
    assert tuple(sorted(shared.__all__)) == _NAMES


@pytest.mark.unit
def test_contract_model_paths_still_resolve() -> None:
    contract = yaml.safe_load(
        (
            _REPO / "src/omnimarket/nodes/node_lane_liveness_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    dotted = contract["input_model"]
    module_name, _, attr = dotted.rpartition(".")
    assert module_name == (
        "omnimarket.nodes.node_lane_liveness_compute.models.model_lane_liveness"
    )
    assert getattr(node_models, attr) is shared.ModelLaneLivenessRequest


@pytest.mark.unit
def test_shared_package_does_not_import_any_node() -> None:
    root = _REPO / "src/omnimarket/models/liveness"
    for path in root.rglob("*.py"):
        assert "omnimarket.nodes" not in path.read_text(encoding="utf-8"), path
