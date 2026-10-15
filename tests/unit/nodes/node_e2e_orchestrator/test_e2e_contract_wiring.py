# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_e2e_orchestrator is wired: its two input topics route to a handler and the bus graph closes.

Before, the contract declared both subscriptions with no handler_routing, so the runtime opened a
dispatcher-less subscription and dropped every message (DECLARED_BUT_UNWIRED). The loop is now
build-completed -> CI read request -> (effect) -> CI read completed -> start or another read request,
and lifecycle-completed -> evidence write, with node_e2e_bridge_effect owning both I/O edges.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import omnibase_infra
import pytest
import yaml

from omnimarket.validators.contract_topic_graph import (
    SCOPE_FENCE,
    ModelContractNode,
    ModelGraphFinding,
    ModelTopicGraph,
    find_defects,
    parse_contract,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_NODES = _ROOT / "src" / "omnimarket" / "nodes"
_ORCH = "node_e2e_orchestrator"
_EFFECT = "node_e2e_bridge_effect"

_BUILD_DONE = "onex.evt.omnimarket.build-loop-orchestrator-completed.v1"
_LIFECYCLE_DONE = "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
_LIFECYCLE_START = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
_READ_REQ = "onex.cmd.omnimarket.build-bridge-ci-read-requested.v1"
_READ_DONE = "onex.evt.omnimarket.build-bridge-ci-read-completed.v1"
_WRITE_REQ = "onex.cmd.omnimarket.build-bridge-evidence-write-requested.v1"
_WRITTEN = "onex.evt.omnimarket.build-bridge-evidence-written.v1"
_E2E_TOPICS = {_READ_REQ, _READ_DONE, _WRITE_REQ, _WRITTEN}


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(raw, dict)
    return raw


def _routes(node: str) -> dict[str, dict[str, Any]]:
    return {h["topic"]: h for h in _contract(node)["handler_routing"]["handlers"]}


def _resolve(module: str, name: str) -> Any:
    return getattr(importlib.import_module(module), name)


class TestOrchestratorRoutesItsInputs:
    def test_both_original_topics_and_the_read_answer_have_a_route(self) -> None:
        assert set(_routes(_ORCH)) == {_BUILD_DONE, _LIFECYCLE_DONE, _READ_DONE}

    def test_every_route_resolves_to_a_handler_class_and_an_event_model(self) -> None:
        for route in _routes(_ORCH).values():
            handler = _resolve(route["handler"]["module"], route["handler"]["name"])
            model = _resolve(
                route["event_model"]["module"], route["event_model"]["name"]
            )
            assert callable(handler.handle)
            assert model.model_config.get("frozen") is True

    def test_subscriptions_publications_and_published_events_agree(self) -> None:
        bus = _contract(_ORCH)["event_bus"]
        assert set(bus["subscribe_topics"]) == {
            _BUILD_DONE,
            _LIFECYCLE_DONE,
            _READ_DONE,
        }
        assert set(bus["publish_topics"]) == {
            _LIFECYCLE_START,
            _READ_REQ,
            _WRITE_REQ,
        }
        published = {
            p["event_type"]: p["topic"] for p in _contract(_ORCH)["published_events"]
        }
        assert set(published.values()) == set(bus["publish_topics"])
        from omnimarket.models import e2e_bridge

        for event_type in published:
            assert hasattr(e2e_bridge, f"Model{event_type}")

    def test_it_is_no_longer_an_unrouted_event_consumer(self) -> None:
        contract = _contract(_ORCH)
        assert "handler_routing" in contract
        assert (
            contract.get("runtime_dispatch", {}).get("invocation_mode")
            != "event_consumer"
        )

    def test_the_imperative_consumer_is_gone(self) -> None:
        assert not (_NODES / _ORCH / "consumer.py").exists()
        for path in (_NODES / _ORCH).rglob("*.py"):
            text = path.read_text("utf-8")
            assert "subprocess" not in text, path
            assert "gh pr checks" not in text, path


class TestEffectOwnsTheIo:
    def test_routes_and_bus(self) -> None:
        assert set(_routes(_EFFECT)) == {_READ_REQ, _WRITE_REQ}
        bus = _contract(_EFFECT)["event_bus"]
        assert set(bus["subscribe_topics"]) == {_READ_REQ, _WRITE_REQ}
        assert set(bus["publish_topics"]) == {_READ_DONE, _WRITTEN}
        for route in _routes(_EFFECT).values():
            handler = _resolve(route["handler"]["module"], route["handler"]["name"])
            _resolve(route["event_model"]["module"], route["event_model"]["name"])
            assert callable(handler.handle)

    @pytest.mark.parametrize("node", [_ORCH, _EFFECT])
    def test_the_node_has_an_entry_point(self, node: str) -> None:
        raw = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))
        points = raw["project"]["entry-points"]["onex.nodes"]
        assert points[node] == f"omnimarket.nodes.{node}"

    def test_the_effect_needs_the_github_token_and_nothing_else(self) -> None:
        assert set(_contract(_EFFECT)["secrets"]) == {"GITHUB_TOKEN"}


def _graph_findings(overrides: dict[str, dict[str, Any]], tmp_path: Path) -> list[Any]:
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
            parsed = parse_contract(source, package)
            if parsed is not None:
                nodes.append(parsed)

    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for contract_node in nodes:
        for topic in contract_node.publish_topics:
            producers.setdefault(topic, []).append(contract_node.name)
        for topic in contract_node.subscribe_topics:
            consumers.setdefault(topic, []).append(contract_node.name)
    graph = ModelTopicGraph(
        nodes=tuple(nodes),
        producers={t: tuple(v) for t, v in producers.items()},
        consumers={t: tuple(v) for t, v in consumers.items()},
        external_producers={
            topic: producer for n in nodes for topic, producer in n.externally_produced
        },
    )
    mine = {
        _contract(_ORCH).get("name", _ORCH),
        _contract(_EFFECT).get("name", _EFFECT),
    }
    found: list[ModelGraphFinding] = find_defects(graph)
    return [f for f in found if f.node in mine or f.topic in _E2E_TOPICS]


class TestTheGraphCloses:
    def test_the_node_is_not_declared_but_unwired(self, tmp_path: Path) -> None:
        assert _graph_findings({}, tmp_path) == []

    def test_the_fence_no_longer_carries_the_node(self) -> None:
        assert not [row for row in SCOPE_FENCE if row[1] == _ORCH]

    def test_positive_control_withholding_the_effect_orphans_the_reads(
        self, tmp_path: Path
    ) -> None:
        effect = _contract(_EFFECT)
        effect["event_bus"]["subscribe_topics"] = []
        found = _graph_findings({_EFFECT: effect}, tmp_path)
        orphaned = {f.topic for f in found if f.defect == "ORPHANED_PRODUCER"}
        assert {_READ_REQ, _WRITE_REQ} <= orphaned

    def test_positive_control_dropping_the_routes_re_declares_it_unwired(
        self, tmp_path: Path
    ) -> None:
        orch = _contract(_ORCH)
        del orch["handler_routing"]
        del orch["handler"]
        found = _graph_findings({_ORCH: orch}, tmp_path)
        assert {"DECLARED_BUT_UNWIRED"} <= {f.defect for f in found}
