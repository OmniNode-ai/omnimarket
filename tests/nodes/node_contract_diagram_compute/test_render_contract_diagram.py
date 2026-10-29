# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""render_contract_diagram: golden output for the plan's fixtures, the view rules and the round trip."""

from __future__ import annotations

import re

import pytest
import yaml

from omnimarket.nodes.node_contract_diagram_compute.handlers.handler_render_contract_diagram import (
    HandlerRenderContractDiagram,
    load_fragment,
)
from omnimarket.nodes.node_contract_diagram_compute.models.model_contract_diagram import (
    EnumDiagramView,
    ModelRenderContractDiagramRequest,
)
from tests.nodes.node_contract_diagram_compute.conftest import (
    PLAN_DIAGRAMS,
    diagram_blocks,
    fragment_blocks,
)

FIXTURE = PLAN_DIAGRAMS.read_text(encoding="utf-8")
FRAGMENTS = fragment_blocks(FIXTURE)
DIAGRAMS = [
    pytest.param(
        node,
        block,
        collapsed,
        lines,
        id=f"{node}-{block}-{'collapsed' if collapsed else 'full'}",
    )
    for node, block, collapsed, lines in diagram_blocks(FIXTURE)
]


def render(fragment: str, view: EnumDiagramView = "full") -> str:
    request = ModelRenderContractDiagramRequest(fragment_text=fragment, view=view)
    return HandlerRenderContractDiagram().handle(request).mermaid


@pytest.mark.parametrize(("node", "block", "collapsed", "lines"), DIAGRAMS)
def test_generated_diagram_equals_the_golden_diagram_byte_for_byte(
    node: str, block: str, collapsed: bool, lines: list[str]
) -> None:
    generated = render(
        FRAGMENTS[f"{node}:{block}"], "collapsed" if collapsed else "full"
    )
    assert generated == "\n".join(lines) + "\n"


# Syntax of every generated line: the Mermaid grammar this generator can emit, line by line.
STATE_LINES = [
    re.compile(r"^stateDiagram-v2$"),
    re.compile(r"^    \[\*\] --> \w+$"),
    re.compile(r"^    \w+ --> \[\*\]$"),
    re.compile(r"^    \w+ --> \w+ : \w+( \(priority \d+\))?( \[[^\[\]]+\])?$"),
]
GRAPH_LINES = [
    re.compile(r"^flowchart TB$"),
    re.compile(r'^    \w+\["[^"]*"\]$'),
    re.compile(r'^    \w+\("[^"]*"\)$'),
    re.compile(r"^    \w+ --> \w+$"),
    re.compile(r'^    \w+ -- "[^"]*" --> \w+$'),
]


@pytest.mark.parametrize(("node", "block", "collapsed", "lines"), DIAGRAMS)
def test_every_generated_line_is_valid_mermaid_syntax(
    node: str, block: str, collapsed: bool, lines: list[str]
) -> None:
    generated = render(
        FRAGMENTS[f"{node}:{block}"], "collapsed" if collapsed else "full"
    )
    grammar = STATE_LINES if block == "state_machine" else GRAPH_LINES
    for line in generated.splitlines():
        assert any(rule.match(line) for rule in grammar), line


def test_both_views_of_the_seam_graph_are_generated() -> None:
    fragment = FRAGMENTS["node_seam_read_orchestrator:graph"]
    full, collapsed = render(fragment, "full"), render(fragment, "collapsed")
    assert full != collapsed
    assert len(full.splitlines()) == 1 + 28 + 27
    # 13 seam readings become arrow labels; the 14th (no station after it) stays a node.
    assert len(collapsed.splitlines()) == 1 + 15 + 1 + 13
    assert collapsed.count(' -- "') == 13


def test_a_graph_without_a_collapsible_node_has_equal_views() -> None:
    fragment = FRAGMENTS["node_delegation_seam_orchestrator:graph"]
    assert render(fragment, "collapsed").count(" -- ") == 8


def test_a_chain_of_two_compute_nodes_is_not_collapsed_into_a_dangling_arrow() -> None:
    fragment = yaml.safe_dump(
        {
            "workflow_coordination": {
                "workflow_definition": {
                    "workflow_metadata": {
                        "workflow_name": "chain",
                        "workflow_version": {"major": 1, "minor": 0, "patch": 0},
                        "description": "two computes in a row",
                    },
                    "execution_graph": {
                        "nodes": [
                            {
                                "node_id": "a",
                                "node_type": "EFFECT_GENERIC",
                                "description": "A",
                            },
                            {
                                "node_id": "b",
                                "node_type": "COMPUTE_GENERIC",
                                "description": "B",
                                "depends_on": ["a"],
                            },
                            {
                                "node_id": "c",
                                "node_type": "COMPUTE_GENERIC",
                                "description": "C",
                                "depends_on": ["b"],
                            },
                            {
                                "node_id": "d",
                                "node_type": "EFFECT_GENERIC",
                                "description": "D",
                                "depends_on": ["c"],
                            },
                        ]
                    },
                }
            }
        }
    )
    collapsed = render(fragment, "collapsed")
    declared = set(re.findall(r"^    (\w+)[\[(]", collapsed, re.M))
    used = set(re.findall(r"^    (\w+) --", collapsed, re.M)) | set(
        re.findall(r"--> (\w+)$", collapsed, re.M)
    )
    assert used <= declared
    assert declared == {"a", "b", "c", "d"}


def test_a_double_quote_in_a_label_is_escaped() -> None:
    fragment = yaml.safe_dump(
        {
            "workflow_coordination": {
                "workflow_definition": {
                    "workflow_metadata": {
                        "workflow_name": "quote",
                        "workflow_version": {"major": 1, "minor": 0, "patch": 0},
                        "description": "a quote",
                    },
                    "execution_graph": {
                        "nodes": [
                            {
                                "node_id": "a",
                                "node_type": "EFFECT_GENERIC",
                                "description": 'say "hi"',
                            }
                        ]
                    },
                }
            }
        }
    )
    assert 'a["say #quot;hi#quot;"]' in render(fragment)


# Round trip: the graph model drops description and step_config, the generator must not ------------


def graph_fragments() -> list[str]:
    return [key for key in FRAGMENTS if key.endswith(":graph")]


@pytest.mark.parametrize("fragment_id", graph_fragments())
def test_every_description_and_operation_survives_from_the_raw_mapping_into_the_diagram(
    fragment_id: str,
) -> None:
    text = FRAGMENTS[fragment_id]
    loaded = load_fragment(text)
    raw_nodes = yaml.safe_load(text)["workflow_coordination"]["workflow_definition"][
        "execution_graph"
    ]["nodes"]

    # the loaded mapping is the raw one, so it keeps what the structural model drops
    assert loaded.kind == "graph"
    assert loaded.nodes == tuple(raw_nodes)
    operations = [
        n["step_config"]["operation"] for n in raw_nodes if "step_config" in n
    ]
    assert operations
    assert [
        n["step_config"]["operation"] for n in loaded.nodes if "step_config" in n
    ] == operations

    diagram = render(text)
    for node in raw_nodes:
        assert f'"{node["description"]}"' in diagram, node["node_id"]


@pytest.mark.parametrize(
    "fragment_id", [key for key in FRAGMENTS if key.endswith(":state_machine")]
)
def test_every_trigger_and_guard_of_a_state_machine_reaches_the_diagram(
    fragment_id: str,
) -> None:
    text = FRAGMENTS[fragment_id]
    diagram = render(text)
    for transition in yaml.safe_load(text)["state_machine"]["transitions"]:
        assert f" : {transition['trigger']}" in diagram
        for condition in transition.get("conditions", []):
            assert condition["expression"] in diagram


def test_a_fragment_that_is_neither_block_is_refused() -> None:
    with pytest.raises(ValueError, match="state_machine"):
        render("something_else: {}\n")


def test_a_fragment_the_core_model_refuses_is_refused() -> None:
    with pytest.raises(ValueError, match="no_such_state"):
        render(
            FRAGMENTS["node_lane_lease_reducer:state_machine"].replace(
                "initial_state: claimed", "initial_state: no_such_state"
            )
        )
