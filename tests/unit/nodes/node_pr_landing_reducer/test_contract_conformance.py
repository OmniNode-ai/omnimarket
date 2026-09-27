# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave-1 seam conformance for the PR landing workflow (OMN-19824).

Four acceptance criteria, one class each:

* AC1: tests/fixtures/pr_landing/fsm_transitions.yaml holds every row of plan
  section 5.1, and the orchestrator contract's ``state_machine`` block equals it
  state for state and edge for edge.
* AC2: payloads recorded off the dev-lane bus (the autobind command and
  ``onex.evt.github.pr-merged.v1``) normalize into ``ModelPrLandingObservation``,
  and a payload whose kind cannot be named is refused.
* AC3: the runtime's own auto-wiring code, run over every node contract in this
  tree, wires no subscription for either new node, on any consumer profile.
* AC4: the four topics this task owns are registered constants, routed by one
  payload class each, and no contract declares a producer for them without a
  consumer (the hard contract-topic-graph gate's rule).
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import omnibase_infra
import pytest
import yaml
from omnibase_core.constants.constants_runtime_profiles import (
    CONSUMER_ATTACHED_RUNTIME_PROFILES,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _prepare_contract_wiring
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    filter_manifest_for_runtime_profile,
)
from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
from pydantic import ValidationError

from omnimarket.events import topics
from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingAgentReason,
    EnumPrLandingIntentKind,
    EnumPrLandingObservationKind,
    EnumPrLandingState,
    ModelPrLandingIntent,
    ModelPrLandingObservation,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_reducer.models import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
)
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
_REDUCER = "node_pr_landing_reducer"
_NEW_NODES = (_ORCHESTRATOR, _REDUCER)
_FIXTURES = _ROOT / "tests" / "fixtures" / "pr_landing"
_TRANSITIONS = _FIXTURES / "fsm_transitions.yaml"
_INGRESS = _FIXTURES / "ingress"

_PLAN_ROWS = {str(n) for n in range(1, 20)} | {"20a", "20b", "21", "22", "23"}

Edge = tuple[str, str, str]


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return cast("dict[str, Any]", loaded)


def _contract(node: str) -> dict[str, Any]:
    return _load_yaml(_NODES / node / "contract.yaml")


def _fixture() -> dict[str, Any]:
    return _load_yaml(_TRANSITIONS)


def _non_terminal(fixture: dict[str, Any]) -> list[str]:
    return [s["name"] for s in fixture["states"] if not s["terminal"]]


def _fixture_edges(fixture: dict[str, Any]) -> set[Edge]:
    alias = fixture["non_terminal_alias"]
    edges: set[Edge] = set()
    for row in [*fixture["transitions"], *fixture["state_table_exits"]]:
        sources = _non_terminal(fixture) if row["from"] == alias else [row["from"]]
        for source in sources:
            edges.add((source, row["trigger"], row["to"]))
    return edges


def _contract_edges(state_machine: dict[str, Any]) -> set[Edge]:
    return {
        (t["from_state"], t["trigger"], t["to_state"])
        for t in state_machine["transitions"]
    }


def _describe(missing: set[Edge], extra: set[Edge]) -> str:
    return (
        f"in the fixture but not the contract: {sorted(missing)}; "
        f"in the contract but not the fixture: {sorted(extra)}"
    )


class TestAc1TransitionTable:
    """The contract state_machine and the frozen fixture are the same machine."""

    def test_the_fixture_holds_every_plan_row(self) -> None:
        rows = [row["row"] for row in _fixture()["transitions"]]
        assert len(rows) == len(set(rows)), "a plan row is transcribed twice"
        assert set(rows) == _PLAN_ROWS

    def test_the_fixture_states_are_the_state_enum(self) -> None:
        names = [s["name"] for s in _fixture()["states"]]
        assert names == [member.value for member in EnumPrLandingState]
        terminal = {s["name"] for s in _fixture()["states"] if s["terminal"]}
        assert terminal == {m.value for m in EnumPrLandingState if m.is_terminal}

    def test_the_contract_state_machine_is_a_valid_core_fsm(self) -> None:
        state_machine = _contract(_ORCHESTRATOR)["state_machine"]
        fsm = ModelFSMSubcontract.model_validate(state_machine)
        assert fsm.initial_state == _fixture()["initial_state"]

    def test_the_contract_states_equal_the_fixture(self) -> None:
        state_machine = _contract(_ORCHESTRATOR)["state_machine"]
        fixture = _fixture()
        assert [s["state_name"] for s in state_machine["states"]] == [
            s["name"] for s in fixture["states"]
        ]
        assert set(state_machine["terminal_states"]) == {
            s["name"] for s in fixture["states"] if s["terminal"]
        }
        for state in state_machine["states"]:
            assert bool(state.get("is_terminal", False)) == (
                state["state_name"] in state_machine["terminal_states"]
            )

    def test_the_contract_transitions_equal_the_fixture(self) -> None:
        contract = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
        fixture = _fixture_edges(_fixture())
        assert contract == fixture, _describe(fixture - contract, contract - fixture)

    def test_deleting_any_contract_transition_is_detected(self) -> None:
        # Positive control for the equality above: it is not vacuous for any edge.
        state_machine = _contract(_ORCHESTRATOR)["state_machine"]
        fixture = _fixture_edges(_fixture())
        assert state_machine["transitions"]
        for index in range(len(state_machine["transitions"])):
            mutated = copy.deepcopy(state_machine)
            del mutated["transitions"][index]
            assert _contract_edges(mutated) != fixture

    def test_adding_a_state_to_the_contract_is_detected(self) -> None:
        state_machine = copy.deepcopy(_contract(_ORCHESTRATOR)["state_machine"])
        state_machine["states"].append(
            {**state_machine["states"][0], "state_name": "UNPLANNED"}
        )
        names = [s["state_name"] for s in state_machine["states"]]
        assert names != [s["name"] for s in _fixture()["states"]]

    def test_every_non_terminal_state_bound_matches_the_fixture(self) -> None:
        state_machine = _contract(_ORCHESTRATOR)["state_machine"]
        bounds = _fixture()["state_bounds_ms"]
        declared = {
            s["state_name"]: s.get("timeout_ms")
            for s in state_machine["states"]
            if not s.get("is_terminal", False)
        }
        assert declared == bounds

    def test_each_state_has_its_frozen_bound(self) -> None:
        # The per-state completion bound, pinned state by state. None means the
        # state is legitimately unbounded (a draft or held PR, or one waiting on
        # an agent); a terminal state has no bound because it never expires.
        expected: dict[EnumPrLandingState, int | None] = {
            EnumPrLandingState.OBSERVED: 300_000,
            EnumPrLandingState.PARKED: None,
            EnumPrLandingState.COMPANION_PENDING: 1_800_000,
            EnumPrLandingState.COMPANION_OPEN: 7_200_000,
            EnumPrLandingState.CHECKS_PENDING: 7_200_000,
            EnumPrLandingState.READY: 900_000,
            EnumPrLandingState.ARMED: 14_400_000,
            EnumPrLandingState.NEEDS_AGENT: None,
            EnumPrLandingState.MERGED: None,
            EnumPrLandingState.CLOSED: None,
        }
        assert set(expected) == set(EnumPrLandingState)
        declared = {
            EnumPrLandingState(s["state_name"]): s.get("timeout_ms")
            for s in _contract(_ORCHESTRATOR)["state_machine"]["states"]
        }
        assert declared == expected

    def test_only_merged_and_closed_are_terminal(self) -> None:
        terminal = {m for m in EnumPrLandingState if m.is_terminal}
        assert terminal == {EnumPrLandingState.MERGED, EnumPrLandingState.CLOSED}
        state_machine = _contract(_ORCHESTRATOR)["state_machine"]
        assert {EnumPrLandingState(s) for s in state_machine["terminal_states"]} == {
            EnumPrLandingState.MERGED,
            EnumPrLandingState.CLOSED,
        }
        # No edge leaves a terminal state; a reopen restarts the row instead.
        sources = {
            EnumPrLandingState(t["from_state"]) for t in state_machine["transitions"]
        }
        assert EnumPrLandingState.MERGED not in sources
        assert EnumPrLandingState.CLOSED not in sources

    def test_every_non_terminal_state_can_reach_needs_agent_and_a_terminal(
        self,
    ) -> None:
        # Row 23 and rows 21/22 cover every non-terminal state, so no state is
        # a trap: each can be handed to an agent, merged or closed.
        edges = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
        for state in (
            EnumPrLandingState.OBSERVED,
            EnumPrLandingState.PARKED,
            EnumPrLandingState.COMPANION_PENDING,
            EnumPrLandingState.COMPANION_OPEN,
            EnumPrLandingState.CHECKS_PENDING,
            EnumPrLandingState.READY,
            EnumPrLandingState.ARMED,
            EnumPrLandingState.NEEDS_AGENT,
        ):
            targets = {to for frm, _trigger, to in edges if frm == state.value}
            assert EnumPrLandingState.NEEDS_AGENT.value in targets, state
            assert EnumPrLandingState.MERGED.value in targets, state
            assert EnumPrLandingState.CLOSED.value in targets, state

    def test_every_fixture_intent_names_a_model_member(self) -> None:
        kinds = {member.value for member in EnumPrLandingIntentKind}
        reasons = {member.value for member in EnumPrLandingAgentReason}
        for row in _fixture()["transitions"]:
            for intent in row["intents"]:
                assert intent["kind"] in kinds, row["row"]
                if intent["kind"] == EnumPrLandingIntentKind.AGENT_NEEDED.value:
                    assert intent["reason"] in reasons, row["row"]
        used = {
            intent["kind"]
            for row in _fixture()["transitions"]
            for intent in row["intents"]
        }
        assert used == kinds, f"intent kinds no row uses: {sorted(kinds - used)}"

    def test_every_agent_reason_is_raised_by_some_row(self) -> None:
        raised = {
            intent["reason"]
            for row in _fixture()["transitions"]
            for intent in row["intents"]
            if intent["kind"] == EnumPrLandingIntentKind.AGENT_NEEDED.value
        }
        assert raised == {member.value for member in EnumPrLandingAgentReason}

    def test_the_contract_keys_state_io_by_landing_key(self) -> None:
        state_io = _contract(_ORCHESTRATOR)["state_io"]
        assert state_io["table"] == "pr_landing_workflow_state"
        assert state_io["key"] == "landing_key"
        assert "landing_key" in ModelPrLandingState.model_fields
        assert "landing_key" in ModelPrLandingObservation.model_fields


def _ingress_fixtures() -> Iterator[Path]:
    yield from sorted(_INGRESS.glob("*.json"))


def _recorded(path: Path) -> tuple[str, dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["recorded"]["topic"], doc["payload"]


class TestAc2IngressNormalization:
    """Recorded live payloads become typed observations; unknown kinds do not."""

    def test_both_live_ingress_topics_have_recorded_payloads(self) -> None:
        recorded_topics = {_recorded(p)[0] for p in _ingress_fixtures()}
        assert recorded_topics == {
            topics.OCC_AUTOBIND_COMMAND_TOPIC_V1,
            topics.PR_MERGED_TOPIC_V1,
        }

    @pytest.mark.parametrize("path", list(_ingress_fixtures()), ids=lambda p: p.stem)
    def test_a_recorded_payload_normalizes(self, path: Path) -> None:
        topic, payload = _recorded(path)
        observation = ModelPrLandingObservation.from_ingress(topic, payload)

        assert observation.repository == payload["repo"]
        assert observation.pr_number == payload["pr_number"]
        # Measured on the live bus: neither ingress carries the head sha.
        assert observation.head_sha is None
        expected = {
            topics.OCC_AUTOBIND_COMMAND_TOPIC_V1: EnumPrLandingObservationKind.PUSHED,
            topics.PR_MERGED_TOPIC_V1: EnumPrLandingObservationKind.MERGED,
        }[topic]
        assert observation.kind is expected
        assert observation.source_topic == topic
        assert observation.landing_key == f"{payload['repo']}#{payload['pr_number']}"
        assert observation.observed_at.tzinfo is not None

    def test_an_observation_round_trips_through_json(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        observation = ModelPrLandingObservation.from_ingress(topic, payload)
        restored = ModelPrLandingObservation.model_validate_json(
            observation.model_dump_json()
        )
        assert restored == observation

    @pytest.mark.parametrize(
        "block_reason",
        [
            r.value
            for r in EnumPrBlockReason
            if r is not EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        ],
    )
    def test_an_autobind_payload_with_another_kind_is_refused(
        self, block_reason: str
    ) -> None:
        path = next(p for p in _ingress_fixtures() if p.stem.startswith("occ_"))
        topic, payload = _recorded(path)
        with pytest.raises(ValueError, match="maps to no landing observation kind"):
            ModelPrLandingObservation.from_ingress(
                topic, {**payload, "block_reason": block_reason}
            )

    def test_an_unknown_block_reason_is_refused(self) -> None:
        path = next(p for p in _ingress_fixtures() if p.stem.startswith("occ_"))
        topic, payload = _recorded(path)
        with pytest.raises(ValidationError):
            ModelPrLandingObservation.from_ingress(
                topic, {**payload, "block_reason": "not_a_reason"}
            )

    def test_a_pr_merged_payload_naming_another_topic_is_refused(self) -> None:
        path = next(p for p in _ingress_fixtures() if p.stem.startswith("pr_merged"))
        topic, payload = _recorded(path)
        with pytest.raises(ValueError, match="names topic"):
            ModelPrLandingObservation.from_ingress(
                topic, {**payload, "topic": "onex.evt.github.pr-closed.v1"}
            )

    def test_an_unknown_ingress_topic_is_refused(self) -> None:
        _topic, payload = _recorded(next(_ingress_fixtures()))
        with pytest.raises(ValueError, match="not a landing ingress topic"):
            ModelPrLandingObservation.from_ingress(
                "onex.evt.github.pr-reopened.v1", payload
            )

    def test_an_unknown_kind_value_is_refused(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        fields = ModelPrLandingObservation.from_ingress(topic, payload).model_dump()
        with pytest.raises(ValidationError):
            ModelPrLandingObservation.model_validate({**fields, "kind": "reopened"})

    def test_the_workflows_own_companion_command_is_not_an_observation(self) -> None:
        path = next(p for p in _ingress_fixtures() if p.stem.startswith("occ_"))
        topic, payload = _recorded(path)
        with pytest.raises(ValueError, match="own companion command"):
            ModelPrLandingObservation.from_ingress(topic, {**payload, "op": "derive"})

    def test_a_mismatched_landing_key_is_refused(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        fields = ModelPrLandingObservation.from_ingress(topic, payload).model_dump()
        with pytest.raises(ValidationError, match="does not match"):
            ModelPrLandingObservation.model_validate(
                {**fields, "landing_key": "OmniNode-ai/other#1"}
            )

    def test_the_reducer_input_refuses_an_observation_for_another_row(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        observation = ModelPrLandingObservation.from_ingress(topic, payload)
        row = ModelPrLandingState.model_validate(
            {
                "repository": observation.repository,
                "pr_number": observation.pr_number + 1,
                "state": EnumPrLandingState.OBSERVED,
                "seq": 0,
                "entered_state_at": observation.observed_at,
            }
        )
        with pytest.raises(ValidationError, match="cannot reduce"):
            ModelPrLandingReduceInput(state=row, observation=observation)

    def test_the_reducer_output_is_a_transition_or_a_drop(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        observation = ModelPrLandingObservation.from_ingress(topic, payload)
        row = ModelPrLandingState.model_validate(
            {
                "repository": observation.repository,
                "pr_number": observation.pr_number,
                "state": EnumPrLandingState.OBSERVED,
                "seq": 0,
                "entered_state_at": observation.observed_at,
            }
        )
        intent = ModelPrLandingIntent(
            kind=EnumPrLandingIntentKind.COMPANION_DERIVE,
            repository=row.repository,
            pr_number=row.pr_number,
        )
        with pytest.raises(ValidationError, match="exactly one of"):
            ModelPrLandingReduceOutput(state=row)
        with pytest.raises(ValidationError, match="no intents"):
            ModelPrLandingReduceOutput(
                state=row, dropped_reason="stale head", intents=(intent,)
            )


def _tree_manifest() -> Any:
    paths = sorted(_NODES.glob("*/contract.yaml"))
    assert len(paths) > 100, "the scan must cover the whole node tree"
    return discover_contracts_from_paths(paths)


class TestAc3NoRuntimeWiring:
    """Discovery sees both nodes; auto-wiring subscribes neither, on any profile."""

    def test_both_nodes_are_registered_for_runtime_discovery(self) -> None:
        pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))
        entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
        for node in _NEW_NODES:
            assert entry_points[node] == f"omnimarket.nodes.{node}"

    def test_runtime_discovery_wires_no_subscription_for_the_new_nodes(self) -> None:
        manifest = _tree_manifest()
        errors = [e for e in manifest.errors if e.entry_point_name in _NEW_NODES]
        assert not errors, errors
        found = {c.name for c in manifest.contracts}
        assert set(_NEW_NODES) <= found

        # Positive control: the same scan does see a wired subscriber, so an
        # empty result below cannot come from a scan that saw nothing.
        merged = next(c for c in manifest.contracts if c.name == "pr_merged_projection")
        assert merged.event_bus is not None
        assert topics.PR_MERGED_TOPIC_V1 in merged.event_bus.subscribe_topics
        assert merged.handler_routing is not None

        owned_somewhere: set[str] = set()
        for profile in sorted(CONSUMER_ATTACHED_RUNTIME_PROFILES):
            owned = filter_manifest_for_runtime_profile(manifest, profile).manifest
            for contract in owned.contracts:
                if contract.name not in _NEW_NODES:
                    continue
                owned_somewhere.add(contract.name)
                prepared = _prepare_contract_wiring(
                    contract=contract,
                    dispatch_engine=object(),
                    resolver=cast("Any", None),
                    ownership_query=object(),
                    event_bus=None,
                    environment="dev",
                )
                assert prepared.subscription_topics == [], (profile, contract.name)
                assert prepared.prepared_wirings == [], (profile, contract.name)
                assert prepared.skip_result is not None
                assert prepared.skip_result.outcome is EnumWiringOutcome.SKIPPED
        # Not vacuous: each node is owned by a consumer profile and was prepared.
        assert owned_somewhere == set(_NEW_NODES)

    @pytest.mark.parametrize("node", _NEW_NODES)
    def test_the_wave_1_contract_declares_no_bus_surface(self, node: str) -> None:
        raw = _contract(node)
        for key in ("handler_routing", "handler", "event_bus", "published_events"):
            assert key not in raw, f"{node} declares {key} before its handler exists"


_OWNED_TOPICS = {
    "PR_LANDING_TRANSITIONED_TOPIC_V1": "onex.evt.omnimarket.pr-landing-transitioned.v1",
    "PR_LANDING_AGENT_NEEDED_TOPIC_V1": "onex.evt.omnimarket.pr-landing-agent-needed.v1",
    "PR_LANDING_MERGED_TOPIC_V1": "onex.evt.omnimarket.pr-landing-merged.v1",
    "PR_LANDING_CLOSED_TOPIC_V1": "onex.evt.omnimarket.pr-landing-closed.v1",
}
_TOPIC_RE = re.compile(r"^onex\.evt\.omnimarket\.[a-z0-9-]+\.v[0-9]+$")


def _publishers_of(topic: str) -> list[str]:
    publishers: list[str] = []
    for path in sorted(_NODES.glob("*/contract.yaml")):
        text = path.read_text(encoding="utf-8")
        if topic not in text:
            continue
        raw = _load_yaml(path)
        bus = raw.get("event_bus") or {}
        declared = list(bus.get("publish_topics") or [])
        declared += [e.get("topic") for e in raw.get("published_events") or []]
        if topic in declared:
            publishers.append(path.parent.name)
    return publishers


def _owned_findings(tmp_path: Path, orchestrator: dict[str, Any]) -> list[Any]:
    """Run the contract-topic-graph defect finder with ``orchestrator`` in place.

    The graph is every omnimarket node contract plus the installed
    omnibase_infra node contracts (the gate's two active runtime packages),
    with the orchestrator contract replaced by ``orchestrator``. Only findings
    on this task's node or its four owned topics are returned; nothing outside
    those two packages can consume a pr-landing topic, so the partial census
    cannot invent an orphan here.
    """
    staged = tmp_path / "omnimarket" / "nodes" / _ORCHESTRATOR / "contract.yaml"
    staged.parent.mkdir(parents=True)
    staged.write_text(yaml.safe_dump(orchestrator, sort_keys=False), "utf-8")

    infra_root = Path(omnibase_infra.__file__).resolve().parent
    nodes: list[ModelContractNode] = []
    for package, paths in (
        ("omnimarket", sorted(_NODES.glob("*/contract.yaml"))),
        ("omnibase_infra", sorted(infra_root.rglob("contract.yaml"))),
    ):
        for path in paths:
            is_orchestrator = (
                package == "omnimarket" and path.parent.name == _ORCHESTRATOR
            )
            node = parse_contract(staged if is_orchestrator else path, package)
            if node is not None:
                nodes.append(node)
    staged_node = next(n for n in nodes if n.name == _ORCHESTRATOR)
    assert staged_node.runtime_loaded, "the staged contract must be gate-eligible"

    producers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for node in nodes:
        for topic in node.publish_topics:
            producers.setdefault(topic, []).append(node.name)
        for topic in node.subscribe_topics:
            consumers.setdefault(topic, []).append(node.name)
    graph = ModelTopicGraph(
        nodes=tuple(nodes),
        producers={t: tuple(v) for t, v in producers.items()},
        consumers={t: tuple(v) for t, v in consumers.items()},
    )
    owned = set(_OWNED_TOPICS.values())
    findings: list[ModelGraphFinding] = find_defects(graph)
    return [f for f in findings if f.node in _NEW_NODES or f.topic in owned]


class TestAc4Topics:
    """The four owned names are registered, routed once, and never orphaned."""

    @pytest.mark.parametrize(("constant", "value"), sorted(_OWNED_TOPICS.items()))
    def test_the_topic_is_a_registered_constant(
        self, constant: str, value: str
    ) -> None:
        assert getattr(topics, constant) == value
        assert _TOPIC_RE.fullmatch(value)

    def test_each_owned_topic_is_routed_by_exactly_one_payload_class(self) -> None:
        routed = list(PR_LANDING_EVENT_TOPICS.values())
        assert sorted(routed) == sorted(_OWNED_TOPICS.values())
        assert len(set(routed)) == len(routed)
        assert {cls.__name__ for cls in PR_LANDING_EVENT_TOPICS} == {
            "ModelPrLandingTransitioned",
            "ModelPrLandingAgentNeeded",
            "ModelPrLandingMerged",
            "ModelPrLandingClosed",
        }

    @pytest.mark.parametrize("topic", sorted(_OWNED_TOPICS.values()))
    def test_at_most_one_contract_publishes_the_topic(self, topic: str) -> None:
        # Exactly one once the handler lands (wave 2); none before, because a
        # declared producer without a consumer fails the contract-topic-graph
        # gate. The next two tests prove that premise against the gate's own
        # defect finder instead of asserting it.
        assert len(_publishers_of(topic)) <= 1

    def test_the_graph_gate_passes_the_wave_1_orchestrator(
        self, tmp_path: Path
    ) -> None:
        findings = _owned_findings(tmp_path, _contract(_ORCHESTRATOR))
        assert findings == []

    def test_the_graph_gate_refuses_a_wave_1_publish_declaration(
        self, tmp_path: Path
    ) -> None:
        # Positive control and the reason the declaration waits for wave 2:
        # the four owned topics declared on the orchestrator today, with no
        # consumer anywhere yet, are four ORPHANED_PRODUCER findings in the
        # HARD --scope omnimarket gate, and a subscription with no
        # handler_routing is DECLARED_BUT_UNWIRED.
        raw = _contract(_ORCHESTRATOR)
        raw["event_bus"] = {
            "version": {"major": 1, "minor": 0, "patch": 0},
            "subscribe_topics": [topics.PR_MERGED_TOPIC_V1],
            "publish_topics": sorted(_OWNED_TOPICS.values()),
        }
        findings = _owned_findings(tmp_path, raw)
        orphaned = {f.topic for f in findings if f.defect == "ORPHANED_PRODUCER"}
        assert orphaned == set(_OWNED_TOPICS.values())
        assert any(f.defect == "DECLARED_BUT_UNWIRED" for f in findings)

    def test_the_bus_seam_in_the_fixture_names_the_owned_topics(self) -> None:
        seam = _fixture()["bus_seam"]
        assert sorted(seam["owned_by_this_task"]) == sorted(_OWNED_TOPICS.values())
        assert set(seam["owned_by_this_task"]) <= set(seam["publish"])
        assert topics.OCC_AUTOBIND_COMMAND_TOPIC_V1 in seam["subscribe"]
        assert topics.PR_MERGED_TOPIC_V1 in seam["subscribe"]
