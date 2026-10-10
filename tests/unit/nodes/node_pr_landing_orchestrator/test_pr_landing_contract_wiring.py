# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR landing workflow's bus graph is declared in its contracts, and it closes.

Wave 2 held the orchestrator's runtime wiring in a test fixture, because each
edge it declares closes only against a sibling's handler, and the HARD
contract-topic-graph gate refuses either half of an edge alone (ledger MSG
2026-09-27T08:27:26Z-pr-landing-w2-T7-83). With the GitHub landing effect
(OMN-19831), the companion outcome (OMN-19832) and the landing projection
(OMN-19833) merged, the integration moves every declaration into the real
contracts in one commit:

* node_pr_landing_orchestrator: runtime_profiles, runtime_lanes, the state_io
  codec, event_bus, published_events and handler_routing, and no experimental
  lifecycle.
* node_pr_landing_github_effect: its command subscription and result
  publications with the handler that answers them, and an entry point.
* node_projection_pr_landing: the four subscriptions T11 withheld, no
  experimental lifecycle, and an entry point.
* node_pr_lifecycle_fix_effect: the companion outcome is no longer an external
  sink, because the orchestrator consumes it.

The last class runs the gate's own defect finder over the omnimarket and
omnibase_infra node trees and asserts no defect touches a PR landing topic or
node, with a positive control that removes one consumer and sees the orphans.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import omnibase_infra
import pytest
import yaml

from omnimarket.events import topics
from omnimarket.validators.contract_topic_graph import (
    ModelContractNode,
    ModelGraphFinding,
    ModelTopicGraph,
    find_defects,
    parse_contract,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_NODES = _ROOT / "src" / "omnimarket" / "nodes"
_ORCHESTRATOR = "node_pr_landing_orchestrator"
_EFFECT = "node_pr_landing_github_effect"
_PROJECTION = "node_projection_pr_landing"
_FIX_EFFECT = "node_pr_lifecycle_fix_effect"
_WIRING_FIXTURE = (
    _ROOT / "tests" / "fixtures" / "pr_landing" / "orchestrator_wiring.yaml"
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_pr_landing_orchestrator.handlers."
    "handler_pr_landing_orchestrator"
)
_INGRESS_MODULE = (
    "omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress"
)
_EFFECT_HANDLER_MODULE = (
    "omnimarket.nodes.node_pr_landing_github_effect.handlers.handler_pr_landing_github"
)

_OWNED_EVENTS = {
    topics.PR_LANDING_TRANSITIONED_TOPIC_V1,
    topics.PR_LANDING_AGENT_NEEDED_TOPIC_V1,
    topics.PR_LANDING_MERGED_TOPIC_V1,
    topics.PR_LANDING_CLOSED_TOPIC_V1,
}
_ORCHESTRATOR_SUBSCRIBES = {
    topics.OCC_AUTOBIND_COMMAND_TOPIC_V1: "ModelPrLandingAutobindPrompt",
    topics.PR_MERGED_TOPIC_V1: "ModelPrLandingMergedIngress",
    topics.PR_LANDING_COMPANION_OUTCOME_TOPIC_V1: "ModelPrLandingCompanionOutcomeIngress",
    topics.PR_LANDING_GITHUB_COMPLETED_TOPIC_V1: "ModelPrLandingGithubCompletedIngress",
    topics.PR_LANDING_GITHUB_FAILED_TOPIC_V1: "ModelPrLandingGithubFailedIngress",
    # OMN-20866: the PR watcher's observations prompt the canary repositories.
    topics.PR_STATE_OBSERVED_TOPIC_V1: "ModelPrLandingObservedPrompt",
}
_ORCHESTRATOR_PUBLISHES = {
    topics.OCC_AUTOBIND_COMMAND_TOPIC_V1,
    topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
    *_OWNED_EVENTS,
}
_EFFECT_RESULTS = {
    topics.PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
    topics.PR_LANDING_GITHUB_FAILED_TOPIC_V1,
}
# Every topic the workflow owns or answers on. A graph defect on any of them,
# or on any of the workflow's nodes, fails the last class.
_WORKFLOW_TOPICS = {
    topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
    topics.PR_LANDING_COMPANION_OUTCOME_TOPIC_V1,
    *_EFFECT_RESULTS,
    *_OWNED_EVENTS,
}


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(raw, dict)
    return raw


def _entry_points() -> dict[str, str]:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))
    entry_points: dict[str, str] = pyproject["project"]["entry-points"]["onex.nodes"]
    return entry_points


class TestTheOrchestratorDeclaresItsWiring:
    def test_the_wiring_fixture_is_gone(self) -> None:
        assert not _WIRING_FIXTURE.exists(), (
            "the wiring block lives in the contract; the staging fixture must go"
        )

    def test_the_orchestrator_is_no_longer_experimental(self) -> None:
        assert "lifecycle" not in _contract(_ORCHESTRATOR)

    def test_it_attaches_on_the_main_runtime_of_the_compose_dev_lane(self) -> None:
        raw = _contract(_ORCHESTRATOR)
        assert raw["runtime_profiles"] == ["main"]
        assert raw["runtime_lanes"] == ["compose-dev"]

    def test_state_io_names_the_table_the_key_and_the_codec(self) -> None:
        state_io = _contract(_ORCHESTRATOR)["state_io"]
        assert state_io["table"] == "pr_landing_workflow_state"
        assert state_io["key"] == "landing_key"
        assert state_io["codec"] == {
            "module": "omnimarket.nodes.node_pr_landing_orchestrator.state_codec",
            "name": "StateIoCodec",
        }

    def test_the_event_bus_subscribes_and_publishes_the_workflow_topics(
        self,
    ) -> None:
        bus = _contract(_ORCHESTRATOR)["event_bus"]
        assert set(bus["subscribe_topics"]) == set(_ORCHESTRATOR_SUBSCRIBES)
        assert set(bus["publish_topics"]) == _ORCHESTRATOR_PUBLISHES

    def test_every_publication_is_routed_by_class_name(self) -> None:
        published = {
            entry["event_type"]: entry["topic"]
            for entry in _contract(_ORCHESTRATOR)["published_events"]
        }
        assert published == {
            "PrLandingTransitioned": topics.PR_LANDING_TRANSITIONED_TOPIC_V1,
            "PrLandingAgentNeeded": topics.PR_LANDING_AGENT_NEEDED_TOPIC_V1,
            "PrLandingMerged": topics.PR_LANDING_MERGED_TOPIC_V1,
            "PrLandingClosed": topics.PR_LANDING_CLOSED_TOPIC_V1,
            "PrLandingGithubRequest": topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
            "PrLifecycleFixCommand": topics.OCC_AUTOBIND_COMMAND_TOPIC_V1,
        }

    def test_every_subscription_routes_to_the_one_handler(self) -> None:
        routing = _contract(_ORCHESTRATOR)["handler_routing"]
        routes = {entry["topic"]: entry for entry in routing["handlers"]}
        assert set(routes) == set(_ORCHESTRATOR_SUBSCRIBES)
        for topic, model in _ORCHESTRATOR_SUBSCRIBES.items():
            entry = routes[topic]
            assert entry["handler"] == {
                "name": "HandlerPrLandingOrchestrator",
                "module": _HANDLER_MODULE,
            }
            assert entry["event_model"] == {"name": model, "module": _INGRESS_MODULE}


class TestTheGithubEffectDeclaresItsBus:
    def test_the_effect_is_no_longer_experimental(self) -> None:
        raw = _contract(_EFFECT)
        assert "lifecycle" not in raw
        assert "seam" not in raw

    def test_it_consumes_the_command_and_publishes_both_results(self) -> None:
        bus = _contract(_EFFECT)["event_bus"]
        assert bus["subscribe_topics"] == [topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1]
        assert set(bus["publish_topics"]) == _EFFECT_RESULTS

    def test_both_results_are_routed_by_class_name(self) -> None:
        published = {
            entry["event_type"]: entry["topic"]
            for entry in _contract(_EFFECT)["published_events"]
        }
        assert published == {
            "PrLandingGithubCompleted": topics.PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
            "PrLandingGithubFailed": topics.PR_LANDING_GITHUB_FAILED_TOPIC_V1,
        }

    def test_the_command_routes_to_the_effect_handler(self) -> None:
        (entry,) = _contract(_EFFECT)["handler_routing"]["handlers"]
        assert entry["topic"] == topics.PR_LANDING_GITHUB_REQUESTED_TOPIC_V1
        assert entry["handler"] == {
            "name": "HandlerPrLandingGithubEffect",
            "module": _EFFECT_HANDLER_MODULE,
        }
        assert entry["event_model"]["name"] == "ModelPrLandingGithubRequest"

    def test_it_attaches_beside_the_orchestrator(self) -> None:
        # The dev lane's effects runtime declares no ONEX_RUNTIME_LANE, so a
        # compose-dev scoped contract there is a fail-closed discovery error.
        # The main runtime declares compose-dev and carries the GitHub token.
        raw = _contract(_EFFECT)
        assert raw["runtime_profiles"] == ["main"]
        assert raw["runtime_lanes"] == ["compose-dev"]
        assert "runtime_profiles" not in raw["descriptor"]


class TestTheProjectionConsumesTheFourEvents:
    def test_the_projection_subscribes_to_the_four_owned_events(self) -> None:
        bus = _contract(_PROJECTION)["event_bus"]
        assert set(bus["subscribe_topics"]) == _OWNED_EVENTS

    def test_the_projection_is_no_longer_experimental(self) -> None:
        assert "lifecycle" not in _contract(_PROJECTION)


class TestRuntimeDiscoveryFindsTheNodes:
    @pytest.mark.parametrize("node", [_ORCHESTRATOR, _EFFECT, _PROJECTION])
    def test_the_node_has_an_entry_point(self, node: str) -> None:
        assert _entry_points()[node] == f"omnimarket.nodes.{node}"


class TestTheCompanionOutcomeHasItsConsumer:
    def test_the_outcome_is_no_longer_declared_an_external_sink(self) -> None:
        external = _contract(_FIX_EFFECT).get("externally_consumed_topics") or []
        assert topics.PR_LANDING_COMPANION_OUTCOME_TOPIC_V1 not in external


def _graph_findings(overrides: dict[str, dict[str, Any]], tmp_path: Path) -> list[Any]:
    """The gate's defect finder over omnimarket plus installed omnibase_infra.

    ``overrides`` replaces a node's contract by name. Only findings on a
    workflow node or topic are returned; nothing outside the two active runtime
    packages consumes or publishes a PR landing topic, so the partial census
    cannot invent an orphan here.
    """
    staged: dict[str, Path] = {}
    for node, raw in overrides.items():
        path = tmp_path / "omnimarket" / "nodes" / node / "contract.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(raw, sort_keys=False), "utf-8")
        staged[node] = path

    infra_root = Path(omnibase_infra.__file__).resolve().parent
    nodes: list[ModelContractNode] = []
    for package, paths in (
        ("omnimarket", sorted(_NODES.glob("*/contract.yaml"))),
        ("omnibase_infra", sorted(infra_root.rglob("contract.yaml"))),
    ):
        for path in paths:
            source = path
            if package == "omnimarket" and path.parent.name in staged:
                source = staged[path.parent.name]
            node = parse_contract(source, package)
            if node is not None:
                nodes.append(node)

    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for node in nodes:
        for topic in node.publish_topics:
            producers.setdefault(topic, []).append(node.name)
        for topic in node.subscribe_topics:
            consumers.setdefault(topic, []).append(node.name)
    # As build_graph does: a consuming contract's externally_produced_topics
    # names the non-contract actor that publishes the topic (the pr-merged
    # publisher workflow, the skill CLI's autobind command).
    external_producers = {
        topic: producer
        for node in nodes
        for topic, producer in node.externally_produced
    }
    graph = ModelTopicGraph(
        nodes=tuple(nodes),
        producers={t: tuple(v) for t, v in producers.items()},
        consumers={t: tuple(v) for t, v in consumers.items()},
        external_producers=external_producers,
    )
    workflow_nodes = {
        _contract(name).get("name", name)
        for name in (_ORCHESTRATOR, _EFFECT, _PROJECTION)
    }
    findings: list[ModelGraphFinding] = find_defects(graph)
    return [
        f for f in findings if f.node in workflow_nodes or f.topic in _WORKFLOW_TOPICS
    ]


class TestTheGraphCloses:
    def test_no_defect_touches_the_workflow(self, tmp_path: Path) -> None:
        assert _graph_findings({}, tmp_path) == []

    def test_every_workflow_topic_has_a_producer_and_a_consumer(
        self, tmp_path: Path
    ) -> None:
        declared: dict[str, tuple[set[str], set[str]]] = {
            topic: (set(), set()) for topic in _WORKFLOW_TOPICS
        }
        for path in sorted(_NODES.glob("*/contract.yaml")):
            node = parse_contract(path, "omnimarket")
            if node is None:
                continue
            for topic in node.publish_topics:
                if topic in declared:
                    declared[topic][0].add(node.name)
            for topic in node.subscribe_topics:
                if topic in declared:
                    declared[topic][1].add(node.name)
        for topic, (producers, consumers) in declared.items():
            assert producers, f"{topic} has no producer"
            assert consumers, f"{topic} has no consumer"

    def test_positive_control_a_withheld_consumer_orphans_the_events(
        self, tmp_path: Path
    ) -> None:
        # The finder is live over this census: withholding the projection's
        # four subscriptions again yields exactly four ORPHANED_PRODUCER.
        projection = _contract(_PROJECTION)
        projection["event_bus"]["subscribe_topics"] = []
        findings = _graph_findings({_PROJECTION: projection}, tmp_path)
        orphaned = {f.topic for f in findings if f.defect == "ORPHANED_PRODUCER"}
        assert orphaned == _OWNED_EVENTS

    def test_positive_control_an_unrouted_subscription_is_unwired(
        self, tmp_path: Path
    ) -> None:
        orchestrator = _contract(_ORCHESTRATOR)
        del orchestrator["handler_routing"]
        findings = _graph_findings({_ORCHESTRATOR: orchestrator}, tmp_path)
        assert any(f.defect == "DECLARED_BUT_UNWIRED" for f in findings)
