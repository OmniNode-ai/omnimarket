# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick compute: the decision node's decision with free slots shared across repositories."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_tick_compute import (
    HandlerPrLandingTick,
    NodePrLandingTickCompute,
    decide_landing_fair_share,
)
from omnimarket.nodes.node_pr_landing_tick_compute.handlers import (
    handler_pr_landing_tick,
)
from omnimarket.nodes.node_pr_landing_tick_compute.handlers.handler_pr_landing_tick import (
    fair_pick,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    LandingWorld,
    run_scenario,
)
from tests.unit.nodes.node_pr_landing_decision_compute.test_landing_decision_guards import (
    RED,
    _pr,
)

NODE_DIR = Path(handler_pr_landing_tick.__file__).resolve().parents[1]
FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)
SPECS = [yaml.safe_load(p.read_text()) for p in sorted(FIXTURES.glob("*.yaml"))]


def _world(prs: list[dict[str, Any]], **policy: Any) -> ModelLandingFacts:
    world = LandingWorld({"id": "fair", "world": {"policy": policy, "prs": prs}})
    world.tick = 1
    return world.facts()


def _dispatched(decision: ModelLandingDecision) -> list[str]:
    return [a.subject for a in decision.actions if a.kind.value == "dispatch_worker"]


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_tick_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelLandingFacts),
        ("output_model", ModelLandingDecision),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingTick
    )
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    assert set(dispatch["terminal_events"]) == {"success", "failure"}


@pytest.mark.unit
def test_handler_is_definition_b() -> None:
    params = list(inspect.signature(HandlerPrLandingTick.handle).parameters.values())
    assert [p.name for p in params] == ["self", "request"]
    assert not inspect.iscoroutinefunction(HandlerPrLandingTick.handle)
    assert issubclass(NodePrLandingTickCompute, HandlerPrLandingTick)
    facts = _world([_pr("acme/app#1", **RED)])
    assert isinstance(HandlerPrLandingTick().handle(facts), ModelLandingDecision)


@pytest.mark.unit
def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_pr_landing_tick.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


@pytest.mark.unit
def test_free_slots_are_shared_across_repositories() -> None:
    facts = _world(
        [
            _pr("acme/app#1", **RED),
            _pr("acme/app#2", **RED),
            _pr("acme/app#3", **RED),
            _pr("acme/lib#9", **RED),
        ],
        max_workers=2,
    )
    assert _dispatched(decide_landing(facts)) == ["acme/app#1", "acme/app#2"]
    assert _dispatched(decide_landing_fair_share(facts)) == [
        "acme/app#1",
        "acme/lib#9",
    ]


@pytest.mark.unit
def test_fair_share_does_not_change_the_facts_or_lose_a_dispatch() -> None:
    facts = _world([_pr("acme/app#1", **RED), _pr("acme/lib#9", **RED)], max_workers=2)
    before = facts.model_dump_json()
    plain = decide_landing(facts)
    fair = decide_landing_fair_share(facts)
    assert facts.model_dump_json() == before
    assert fair.model_dump_json() == plain.model_dump_json()


@pytest.mark.unit
def test_fair_pick_prefers_exempt_then_the_repo_with_fewest_leases() -> None:
    candidates = ["a/x#1", "a/x#2", "b/y#3", "c/z#4"]
    assert fair_pick(candidates, [], 2, set()) == ["a/x#1", "b/y#3"]
    assert fair_pick(candidates, ["a/x#9", "b/y#8"], 2, set()) == ["c/z#4", "a/x#1"]
    assert fair_pick(candidates, [], 2, {"c/z#4"}) == ["c/z#4", "a/x#1"]


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_every_reviewed_scenario_tick_gives_the_plain_decision(
    spec: dict[str, Any],
) -> None:
    """The reviewed scenarios are one-repo worlds: fair share must leave them byte-identical."""
    for facts in run_scenario(spec).facts_seen:
        assert (
            decide_landing_fair_share(facts).model_dump_json()
            == decide_landing(facts).model_dump_json()
        )
