# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay keys ``landing_goal`` and ``repo_scope`` of the landing nodes (rebuild piece 9, OMN-22918).

The shipped example parses; the loader finds the node's overlay under a named root; an absent key, an unknown key and
a bound of zero each refuse the load and name the field; the scope keeps only listed repositories of the visibility
it names. The named breaks (a key removed, a bound set to zero, a repository left out) turn the matching assertion red.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_goal_overlay import (
    ModelPrLandingGoalOverlay,
)
from omnimarket.nodes.node_pr_landing_orchestrator.overlay import (
    NODE_NAME,
    load_landing_goal_overlay,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_landing_orchestrator/overlay.example.yaml"
)


def _document() -> dict[str, object]:
    loaded = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _goal(document: dict[str, object], **changes: object) -> dict[str, object]:
    goal = document["landing_goal"]
    assert isinstance(goal, dict)
    return {**goal, **changes}


def _root(tmp_path: Path, document: dict[str, object]) -> dict[str, str]:
    node_dir = tmp_path / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


def test_the_shipped_example_is_a_valid_overlay() -> None:
    overlay = ModelPrLandingGoalOverlay.model_validate(_document())
    assert overlay.landing_goal.merges_per_window == 10
    assert overlay.landing_goal.eligible_head_bound_seconds == 3600
    assert overlay.repo_scope.visibility == "all"


def test_the_loader_reads_the_nodes_overlay_under_the_named_root(
    tmp_path: Path,
) -> None:
    document = _document()
    document["landing_goal"] = _goal(document, merges_per_window=12)
    overlay = load_landing_goal_overlay(_root(tmp_path, document))
    assert overlay.landing_goal.merges_per_window == 12


def test_no_overlay_under_any_root_refuses_and_names_the_node(tmp_path: Path) -> None:
    with pytest.raises(NodeOverlayNotFoundError, match=NODE_NAME):
        load_landing_goal_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})


@pytest.mark.parametrize("key", ["landing_goal", "repo_scope"])
def test_an_absent_key_refuses_the_load_and_names_it(tmp_path: Path, key: str) -> None:
    document = _document()
    del document[key]
    with pytest.raises(NodeOverlayError, match=key):
        load_landing_goal_overlay(_root(tmp_path, document))


def test_an_unknown_key_refuses_the_load(tmp_path: Path) -> None:
    document = _document()
    document["landing_goal"] = _goal(document, extra_goal=1)
    with pytest.raises(NodeOverlayError, match="extra_goal"):
        load_landing_goal_overlay(_root(tmp_path, document))


@pytest.mark.parametrize(
    "field",
    [
        "merges_per_window",
        "window_seconds",
        "waiting_threshold",
        "eligible_head_bound_seconds",
        "canary_bound_seconds",
    ],
)
def test_a_goal_field_of_zero_or_absent_refuses_the_load(
    tmp_path: Path, field: str
) -> None:
    zero = _document()
    zero["landing_goal"] = _goal(zero, **{field: 0})
    with pytest.raises(NodeOverlayError, match=field):
        load_landing_goal_overlay(_root(tmp_path / "zero", zero))
    absent = _document()
    goal = _goal(absent)
    del goal[field]
    absent["landing_goal"] = goal
    with pytest.raises(NodeOverlayError, match=field):
        load_landing_goal_overlay(_root(tmp_path / "absent", absent))


def test_an_empty_scope_and_an_unknown_visibility_refuse_the_load(
    tmp_path: Path,
) -> None:
    empty = _document()
    empty["repo_scope"] = {"repositories": [], "visibility": "all"}
    with pytest.raises(NodeOverlayError, match="repositories"):
        load_landing_goal_overlay(_root(tmp_path / "empty", empty))
    odd = _document()
    odd["repo_scope"] = {
        "repositories": ["example-org/example-repo-a"],
        "visibility": "some",
    }
    with pytest.raises(NodeOverlayError, match="visibility"):
        load_landing_goal_overlay(_root(tmp_path / "odd", odd))


def test_scope_keeps_only_listed_repositories_of_the_visibility_it_names() -> None:
    scope = ModelPrLandingGoalOverlay.model_validate(_document()).repo_scope
    assert scope.in_scope("example-org/example-repo-a", public=True)
    assert scope.in_scope("example-org/example-repo-a", public=False)
    assert not scope.in_scope("example-org/unlisted", public=True)
    public_only = scope.model_copy(update={"visibility": "public"})
    assert public_only.in_scope("example-org/example-repo-a", public=True)
    assert not public_only.in_scope("example-org/example-repo-a", public=False)
    private_only = scope.model_copy(update={"visibility": "private"})
    assert private_only.in_scope("example-org/example-repo-b", public=False)
    assert not private_only.in_scope("example-org/example-repo-b", public=True)


def test_named_break_a_repository_left_out_of_the_scope_is_out_of_scope() -> None:
    """The failing control: drop a listed repository and the in-scope assertion goes red."""
    scope = ModelPrLandingGoalOverlay.model_validate(_document()).repo_scope
    assert scope.in_scope("example-org/example-repo-b", public=True)
    broken = scope.model_copy(update={"repositories": ("example-org/example-repo-a",)})
    with pytest.raises(AssertionError):
        assert broken.in_scope("example-org/example-repo-b", public=True)
