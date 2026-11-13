# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared code eval trial models live in omnimarket.models, not node packages."""

from pathlib import Path

import pytest

import omnimarket.models.code_eval_trial
from omnimarket.models.model_code_score import (
    ModelCodeScored as SharedModelCodeScored,
)
from omnimarket.models.model_code_score import (
    ModelCodeScoreOverlay as SharedModelCodeScoreOverlay,
)
from omnimarket.models.model_code_score import (
    ModelCodeScoreRequest as SharedModelCodeScoreRequest,
)
from omnimarket.models.model_lab_pool_place import (
    EnumPlacementDecision as SharedEnumPlacementDecision,
)
from omnimarket.models.model_lab_pool_place import (
    ModelLabPoolPlaceRequest as SharedModelLabPoolPlaceRequest,
)
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScored as NodeModelCodeScored,
)
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScoreOverlay as NodeModelCodeScoreOverlay,
)
from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScoreRequest as NodeModelCodeScoreRequest,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_pool_place import (
    EnumPlacementDecision as NodeEnumPlacementDecision,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_pool_place import (
    ModelLabPoolPlaceRequest as NodeModelLabPoolPlaceRequest,
)

pytestmark = pytest.mark.unit

NODE_DIRECTORIES = (
    "node_code_eval_trial_orchestrator",
    "node_code_eval_trial_reducer",
)


def test_old_score_and_placement_paths_are_the_same_classes() -> None:
    assert NodeModelCodeScoreRequest is SharedModelCodeScoreRequest
    assert NodeModelCodeScored is SharedModelCodeScored
    assert NodeModelCodeScoreOverlay is SharedModelCodeScoreOverlay
    assert NodeModelLabPoolPlaceRequest is SharedModelLabPoolPlaceRequest
    assert NodeEnumPlacementDecision is SharedEnumPlacementDecision


def test_code_eval_trial_package_exports_resolve() -> None:
    exports = omnimarket.models.code_eval_trial.__all__
    assert exports
    for name in exports:
        obj = getattr(omnimarket.models.code_eval_trial, name)
        if isinstance(obj, type):
            assert obj.__module__.startswith("omnimarket.models.code_eval_trial.")


def test_orchestrator_and_reducer_node_packages_define_no_models() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"
    for node in NODE_DIRECTORIES:
        assert not (src / node / "models").is_dir()


def test_node_sources_import_models_only_from_the_shared_package() -> None:
    src = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"
    checked = 0
    for node in NODE_DIRECTORIES:
        for path in sorted((src / node).rglob("*.py")):
            checked += 1
            text = path.read_text(encoding="utf-8")
            assert "node_code_eval_trial_orchestrator.models" not in text
            assert "node_code_eval_trial_reducer.models" not in text
    assert checked > 0
