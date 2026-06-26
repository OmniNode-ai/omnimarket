# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-sourced topic wiring tests for node_delegation_orchestrator (OMN-13193).

Workstream A, phase A3: the orchestrator's publish/subscribe topic constants are
resolved from the node's ``contract.yaml`` via the adopted
``contract_publish_topics`` / ``contract_subscribe_topics`` helpers, never
imported from the infra event-bus topic constants. These tests prove each
resolved string equals its contract declaration and that the migrated source
files own no topic literals or infra topic-constant imports.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_delegation_orchestrator import contract_topics

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODE_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_delegation_orchestrator"
_CONTRACT = _NODE_DIR / "contract.yaml"
_MIGRATED_SOURCES = (
    _NODE_DIR / "contract_topics.py",
    _NODE_DIR / "dispatchers" / "dispatcher_delegation_workflow.py",
    _NODE_DIR / "dispatchers" / "dispatcher_routing_decision.py",
    _NODE_DIR / "handlers" / "handler_delegation_workflow.py",
)


def _contract_event_bus() -> dict[str, list[str]]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    event_bus = raw["event_bus"]
    assert isinstance(event_bus, dict)
    return event_bus


@pytest.mark.unit
def test_subscribe_topic_constants_match_contract() -> None:
    subscribe_topics = _contract_event_bus()["subscribe_topics"]
    assert isinstance(subscribe_topics, list)

    assert contract_topics.TOPIC_ID_INVOCATION_COMMAND in subscribe_topics
    assert contract_topics.TOPIC_ID_AGENT_TASK_LIFECYCLE in subscribe_topics
    assert (
        contract_topics.TOPIC_ID_INVOCATION_COMMAND
        == "onex.cmd.omnibase-infra.invocation.v1"  # onex-topic-allow: equality proof
    )
    assert (
        contract_topics.TOPIC_ID_AGENT_TASK_LIFECYCLE
        == "onex.evt.omnibase-infra.agent-task-lifecycle.v1"  # onex-topic-allow: equality proof
    )


@pytest.mark.unit
def test_publish_topic_constants_match_contract() -> None:
    publish_topics = _contract_event_bus()["publish_topics"]
    assert isinstance(publish_topics, list)

    # Locked expected values (migration-lock). These equal the prior
    # omnibase_infra.event_bus.topic_constants literals; if contract.yaml and the
    # resolver drift together, membership-only checks would still pass — the
    # fixed values catch a cross-service topic-contract change. (CodeRabbit 1241.)
    # OMN-13629: TOPIC_ID_TASK_DELEGATED was removed — the orchestrator no longer
    # publishes the legacy compat task-delegated.v1.
    expected = {
        "inference": contract_topics.TOPIC_ID_INFERENCE_REQUEST,
        "quality_gate": contract_topics.TOPIC_ID_QUALITY_GATE_REQUEST,
        "routing": contract_topics.TOPIC_ID_ROUTING_REQUEST,
        "completed": contract_topics.TOPIC_ID_DELEGATION_COMPLETED,
        "failed": contract_topics.TOPIC_ID_DELEGATION_FAILED,
    }
    assert (
        expected["inference"]
        == (
            "onex.cmd.omnibase-infra.delegation-inference-request.v1"  # onex-topic-allow: equality proof
        )
    )
    assert (
        expected["quality_gate"]
        == (
            "onex.cmd.omnibase-infra.delegation-quality-gate-request.v1"  # onex-topic-allow: equality proof
        )
    )
    assert (
        expected["routing"]
        == (
            "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: equality proof
        )
    )
    assert (
        expected["completed"]
        == (
            "onex.evt.omnibase-infra.delegation-completed.v1"  # onex-topic-allow: equality proof
        )
    )
    assert (
        expected["failed"]
        == (
            "onex.evt.omnibase-infra.delegation-failed.v1"  # onex-topic-allow: equality proof
        )
    )
    for resolved in expected.values():
        assert resolved in publish_topics
    # OMN-13629: the legacy compat topic must NOT be in publish_topics.
    assert (
        "onex.evt.omniclaude.task-delegated.v1"  # onex-topic-allow: negative proof
        not in publish_topics
    )


@pytest.mark.unit
def test_single_topic_rejects_ambiguous_match() -> None:
    with pytest.raises(ValueError, match="expected exactly one"):
        contract_topics._single_topic(
            ("a.v1", "a.v1"), "a.v1", section="publish_topics"
        )
    with pytest.raises(ValueError, match="expected exactly one"):
        contract_topics._single_topic((), "missing", section="subscribe_topics")


@pytest.mark.unit
def test_migrated_sources_own_no_topic_literals_or_constant_imports() -> None:
    for source_path in _MIGRATED_SOURCES:
        source = source_path.read_text(encoding="utf-8")
        assert "from omnibase_infra.event_bus.topic_constants import" not in source, (
            f"{source_path} still imports infra topic constants"
        )
        # The orchestrator topics are contract-declared on the omnibase-infra /
        # omniclaude services; the migrated files must not own those literals.
        for literal_prefix in ("onex.cmd.omnibase-infra", "onex.evt.omni"):
            assert literal_prefix not in source, (
                f"{source_path} owns a hardcoded topic literal "
                f"starting {literal_prefix!r}"
            )
