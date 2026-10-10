# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe mode: a copied tick replays equal through the node path, and a drift is named."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_tick_compute import decide_landing_fair_share
from omnimarket.nodes.node_pr_landing_tick_observe_compute import (
    HandlerPrLandingTickObserve,
    NodePrLandingTickObserveCompute,
    diff_paths,
)
from omnimarket.nodes.node_pr_landing_tick_observe_compute.handlers import (
    handler_pr_landing_tick_observe,
)
from omnimarket.nodes.node_pr_landing_tick_observe_compute.models.model_landing_tick_observation import (
    ModelLandingTickComparison,
    ModelLandingTickObservation,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    LandingWorld,
    run_scenario,
)
from tests.unit.nodes.node_pr_landing_decision_compute.test_landing_decision_guards import (
    RED,
    _pr,
)

NODE_DIR = Path(handler_pr_landing_tick_observe.__file__).resolve().parents[1]
FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)
SPECS = [yaml.safe_load(p.read_text()) for p in sorted(FIXTURES.glob("*.yaml"))]


def _facts(prs: list[dict[str, Any]], **policy: Any) -> ModelLandingFacts:
    world = LandingWorld({"id": "obs", "world": {"policy": policy, "prs": prs}})
    world.tick = 1
    return world.facts()


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_tick_observe_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    for side, model in (
        ("input_model", ModelLandingTickObservation),
        ("output_model", ModelLandingTickComparison),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingTickObserve
    )
    assert contract["runtime_dispatch"]["command_topic"].startswith(
        "onex.cmd.omnimarket."
    )


@pytest.mark.unit
def test_handler_is_definition_b_and_does_no_io() -> None:
    params = list(
        inspect.signature(HandlerPrLandingTickObserve.handle).parameters.values()
    )
    assert [p.name for p in params] == ["self", "request"]
    assert issubclass(NodePrLandingTickObserveCompute, HandlerPrLandingTickObserve)
    tree = ast.parse(Path(handler_pr_landing_tick_observe.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_every_reviewed_scenario_tick_observes_equal(spec: dict[str, Any]) -> None:
    for facts in run_scenario(spec).facts_seen:
        for fair_share in (True, False):
            controller = (
                decide_landing_fair_share(facts)
                if fair_share
                else decide_landing(facts)
            )
            verdict = HandlerPrLandingTickObserve().handle(
                ModelLandingTickObservation(
                    facts=facts, controller_decision=controller, fair_share=fair_share
                )
            )
            assert verdict.equal, verdict.differences
            assert verdict.tick == facts.tick
            assert verdict.controller_actions == verdict.node_actions


@pytest.mark.unit
def test_a_controller_that_did_not_share_slots_is_named_as_a_drift() -> None:
    facts = _facts(
        [
            _pr("acme/app#1", **RED),
            _pr("acme/app#2", **RED),
            _pr("acme/lib#9", **RED),
        ],
        max_workers=2,
    )
    plain = decide_landing(facts)
    verdict = HandlerPrLandingTickObserve().handle(
        ModelLandingTickObservation(facts=facts, controller_decision=plain)
    )
    assert not verdict.equal
    assert verdict.controller_actions == verdict.node_actions == 2
    assert any("subject" in d.path for d in verdict.differences)
    assert any("acme/app#2" in d.controller for d in verdict.differences)
    assert any("acme/lib#9" in d.node for d in verdict.differences)
    unshared = HandlerPrLandingTickObserve().handle(
        ModelLandingTickObservation(
            facts=facts, controller_decision=plain, fair_share=False
        )
    )
    assert unshared.equal


@pytest.mark.unit
def test_diff_paths_names_added_removed_and_changed_values() -> None:
    differences = diff_paths(
        {"a": 1, "b": [1, 2], "c": {"d": "x"}, "e": 1},
        {"a": 2, "b": [1], "c": {"d": "x", "f": 3}, "g": 1},
    )
    paths = {d.path: (d.controller, d.node) for d in differences}
    assert paths == {
        "$.a": ("1", "2"),
        "$.b[1]": ("2", "<absent>"),
        "$.c.f": ("<absent>", "3"),
        "$.e": ("1", "<absent>"),
        "$.g": ("<absent>", "1"),
    }
    assert diff_paths({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []
