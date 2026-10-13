# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared pipeline exports close all four publisher edges; deletion breaks them."""

from pathlib import Path

import pytest

from omnimarket.validators.contract_topic_graph import (
    ModelTopicGraph,
    find_defects,
    parse_contract,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_pipeline_exports_close_both_publishers_with_four_edge_negative_control() -> (
    None
):
    nodes = []
    for name in ("node_emit_daemon", "node_event_emit_effect"):
        path = ROOT / "src/omnimarket/nodes" / name / "contract.yaml"
        node = parse_contract(path, "omnimarket")
        assert node is not None
        topics = tuple(
            t
            for t in node.publish_topics
            if t.endswith(("budget-cap-hit.v1", "circuit-breaker-tripped.v1"))
        )
        assert len(topics) == 2
        nodes.append(
            node.model_copy(
                update={
                    "publish_topics": topics,
                    "subscribe_topics": (),
                    "command_topic": None,
                }
            )
        )
    producers = {t: tuple(n.name for n in nodes) for t in nodes[0].publish_topics}
    graph = ModelTopicGraph(nodes=tuple(nodes), producers=producers)
    assert not [f for f in find_defects(graph) if f.defect == "ORPHANED_PRODUCER"]
    control = graph.model_copy(
        update={
            "nodes": tuple(
                n.model_copy(update={"externally_consumed": ()}) for n in nodes
            )
        }
    )
    failures = [f for f in find_defects(control) if f.defect == "ORPHANED_PRODUCER"]
    assert len(failures) == 4
    assert {f.node for f in failures} == {n.name for n in nodes}
