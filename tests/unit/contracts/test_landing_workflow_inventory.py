# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing workflow contract and the controller inventory (OMN-23393, rebuild piece 9).

The workflow contract composes one landing tick from existing nodes; every step must name a
node that exists and the command topic that node's own contract declares. The inventory maps
every controller function to an existing node, a node to build, or dead code; its counts must
match its rows, and every node an "exists" row names in omnimarket must exist. Every recorded
scenario tick (S1 to S20 and the later ones) must replay equal through the tick orchestrator.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"
WORKFLOW = (
    REPO
    / "src"
    / "omnimarket"
    / "contracts"
    / "landing_workflow"
    / "landing_workflow.v1.yaml"
)
INVENTORY = REPO / "docs" / "reference" / "landing_controller_inventory.yaml"
REPLAY = REPO / "tests" / "fixtures" / "pr_landing_tick_replay"
CLASSES = {"exists", "to_build", "dead"}
WHERE = {"omnimarket", "runtime", "private", "-"}


def _load(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _workflow() -> dict[str, object]:
    return _load(WORKFLOW)


def _rows() -> list[dict[str, object]]:
    rows = _load(INVENTORY)["rows"]
    assert isinstance(rows, list)
    return rows


def test_every_workflow_step_names_an_existing_node_and_its_own_command_topic() -> None:
    steps = _workflow()["steps"]
    assert isinstance(steps, list)
    assert steps
    for step in steps:
        contract = NODES / step["node"] / "contract.yaml"
        assert contract.is_file(), f"step {step['id']}: no node {step['node']}"
        topic = step.get("command_topic")
        if topic:
            assert topic in contract.read_text(encoding="utf-8"), (
                f"step {step['id']}: {step['node']} declares no topic {topic}"
            )


def test_the_workflow_writes_only_through_the_github_landing_effect() -> None:
    workflow = _workflow()
    writers = [s for s in workflow["steps"] if s["id"] == "write"]
    assert [s["node"] for s in writers] == ["node_pr_landing_github_effect"]
    assert workflow["overlay"]["keys"]["act_enabled"].startswith("false")


def test_to_build_nodes_are_not_built_yet_and_private_nodes_are_not_in_omnimarket() -> (
    None
):
    workflow = _workflow()
    for item in workflow["to_build"]:
        node = item.get("node")
        if node:
            assert not (NODES / node).exists(), f"{node} exists: move it to steps"
        else:
            assert (NODES / item["host_node"] / "contract.yaml").is_file()
    for node in workflow["private_nodes_to_move"]:
        if node == "node_idle_slot_fill_effect":
            continue  # the omnimarket node exists; only the private copy's scope reads move
        assert not (NODES / node).exists(), f"{node} already moved: list it as a step"


def test_the_inventory_counts_match_its_rows_and_every_function_is_classed_once() -> (
    None
):
    inventory = _load(INVENTORY)
    rows = _rows()
    names = [str(r["function"]) for r in rows]
    assert len(names) == len(set(names)) == inventory["functions"]
    for row in rows:
        assert row["class"] in CLASSES, row
        assert row["where"] in WHERE, row
        assert re.fullmatch(r"\d+-\d+", str(row["lines"])), row
        assert str(row["maps_to"]).strip(), row
    by_class = Counter(str(r["class"]) for r in rows)
    by_where = Counter(str(r["where"]) for r in rows if r["class"] == "exists")
    counts = inventory["counts"]
    assert counts["exists"] == by_class["exists"]
    assert counts["to_build"] == by_class["to_build"]
    assert counts["dead"] == by_class["dead"]
    assert counts["exists_omnimarket"] == by_where["omnimarket"]
    assert counts["exists_runtime"] == by_where["runtime"]
    assert counts["exists_private_to_move"] == by_where["private"]


def test_every_node_an_inventory_row_names_exists_or_is_declared_by_the_workflow() -> (
    None
):
    workflow = _workflow()
    declared = {i["node"] for i in workflow["to_build"] if i.get("node")}
    private = set(workflow["private_nodes_to_move"])
    for row in _rows():
        for node in row["nodes"]:
            if row["where"] == "omnimarket" and row["class"] == "exists":
                assert (NODES / node / "contract.yaml").is_file(), (
                    row["function"],
                    node,
                )
            elif row["where"] == "private":
                assert node in private, (row["function"], node)
            elif row["class"] == "to_build":
                assert node in declared or (NODES / node).is_dir(), (
                    row["function"],
                    node,
                )


SCENARIOS = sorted(REPLAY.glob("*.json"))


def test_the_recorded_scenarios_s1_to_s20_are_all_present() -> None:
    numbers = {
        int(m.group(1))
        for p in SCENARIOS
        if (m := re.match(r"scenario_s(\d+)", p.name))
    }
    assert set(range(1, 21)) <= numbers


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_every_recorded_tick_replays_equal_through_the_workflow(path: Path) -> None:
    snapshot = ModelTickSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    publisher = InMemoryTickPublisher()
    result = HandlerPrLandingTickOrchestrator(publisher=publisher).handle(
        ModelTickRequest(
            correlation_id=uuid4(),
            mode=EnumTickMode.OBSERVE,
            facts=snapshot.facts,
            recorded_decision=snapshot.decision,
            fair_share=snapshot.fair_share,
        )
    )
    assert result.status is EnumTickStatus.COMPARED
    assert result.equal is True, (
        f"{path.name} diverges on {[d.subject for d in result.divergences]}"
    )
    assert all(e.writes_performed == 0 for _, e in publisher.published)
