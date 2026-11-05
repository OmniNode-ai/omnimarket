# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded review command has a target, but no sender."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.validators.contract_topic_graph import (
    ModelTopicGraph,
    find_defects,
    parse_contract,
)

TOPIC = "onex.cmd.omnimarket.agent-review-requested.v1"
CONTRACT = Path(
    "src/omnimarket/nodes/node_agent_review_dispatch_orchestrator/contract.yaml"
)


def _review(tmp_path: Path, invocation: object = None):
    raw = yaml.safe_load(CONTRACT.read_text())
    raw["runtime_dispatch"].pop("invocation", None)
    if invocation is not None:
        raw["runtime_dispatch"]["invocation"] = invocation
    path = tmp_path / "omnimarket/nodes/node_review/contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw))
    return parse_contract(path, package="omnimarket")


def test_recorded_review_target_without_sender_is_orphaned(tmp_path: Path) -> None:
    node = _review(tmp_path)
    assert node is not None
    assert node.runtime_loaded
    assert node.command_topic == TOPIC
    graph = ModelTopicGraph(nodes=(node,), consumers={TOPIC: (node.name,)})
    assert not graph.is_reachable(TOPIC)
    assert any(f.defect == "ORPHANED_CONSUMER" for f in find_defects(graph))


def test_real_contract_publisher_positive_control(tmp_path: Path) -> None:
    node = _review(tmp_path)
    assert node is not None
    graph = ModelTopicGraph(nodes=(node,), producers={TOPIC: ("review_scheduler",)})
    assert graph.is_reachable(TOPIC)


def test_explicit_on_demand_names_sender_and_reason(tmp_path: Path) -> None:
    node = _review(
        tmp_path,
        {
            "mode": "on_demand",
            "sender": "operator via onex node node_agent_review_dispatch_orchestrator",
            "reason": "Automatic review producer is unbound; manual diagnosis only.",
        },
    )
    assert node is not None
    assert ModelTopicGraph(nodes=(node,)).is_reachable(TOPIC)


@pytest.mark.parametrize(
    "invocation",
    [
        {"mode": "on_demand"},
        {"mode": "on_demand", "sender": " ", "reason": "diagnosis"},
        {"mode": "on_demand", "sender": "operator", "reason": ""},
        {"mode": "automatic", "sender": ""},
        {"mode": "invented", "sender": "operator", "reason": "diagnosis"},
    ],
)
def test_invalid_sender_declaration_fails_closed(
    tmp_path: Path, invocation: object
) -> None:
    with pytest.raises(ValidationError, match="validation error"):
        _review(tmp_path, invocation)
