# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wave-1 seam conformance for the PR landing workflow (OMN-19824).

Four acceptance criteria, one class each:

* AC1: tests/fixtures/pr_landing/fsm_transitions.yaml holds every row of the
  model-checked revision of plan section 5.1 (revision 1, section 3: F1-F10,
  R1-R4, G5), the orchestrator contract's ``state_machine`` block equals it
  state for state and edge for edge, and every counterexample case in
  tests/fixtures/pr_landing/counterexamples.yaml replays on those edges.
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
    ModelPrLandingClosed,
    ModelPrLandingIntent,
    ModelPrLandingMerged,
    ModelPrLandingObservation,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_intent_kind import (
    ORCHESTRATOR_ISSUED_INTENT_KINDS,
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
_COUNTEREXAMPLES = _FIXTURES / "counterexamples.yaml"
_INGRESS = _FIXTURES / "ingress"

_REVISION_ROWS = 43
_PLAN_ROWS = {str(n) for n in range(1, _REVISION_ROWS + 1)}
_REVISION_CHANGES = {
    *(f"F{n}" for n in range(1, 11)),
    "R1",
    "R2a",
    "R2b",
    "R2c",
    "R3",
    "R4",
    "G5",
}
_DISARM_CASES = {"armed", "unsent", "in_flight", "closed"}

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


def _sources(fixture: dict[str, Any], row: dict[str, Any]) -> list[str]:
    source = row["from"]
    if source == fixture["non_terminal_alias"]:
        return _non_terminal(fixture)
    return list(source) if isinstance(source, list) else [source]


def _triggers(row: dict[str, Any]) -> list[str]:
    return list(row.get("triggers") or [row["trigger"]])


def _row_edges(fixture: dict[str, Any], row: dict[str, Any]) -> set[Edge]:
    edges: set[Edge] = set()
    for source in _sources(fixture, row):
        target = source if row["to"] == fixture["unchanged_alias"] else row["to"]
        for trigger in _triggers(row):
            edges.add((source, trigger, target))
    return edges


def _fixture_edges(fixture: dict[str, Any]) -> set[Edge]:
    edges: set[Edge] = set()
    for row in fixture["transitions"]:
        edges |= _row_edges(fixture, row)
    return edges


def _row_intents(fixture: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    """Intent kinds per (from, trigger), for the counterexample walk."""
    kinds: dict[tuple[str, str], set[str]] = {}
    for row in fixture["transitions"]:
        for source, trigger, _to in _row_edges(fixture, row):
            kinds[(source, trigger)] = {i["kind"] for i in row["intents"]}
    return kinds


def _all_intents(fixture: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (row["row"], intent)
        for section in ("transitions", "closed_episode_rules")
        for row in fixture[section]
        for intent in row["intents"]
    ]


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
        fixture = _fixture()
        assert fixture["revision_rows"] == _REVISION_ROWS
        edges = [row["row"] for row in fixture["transitions"]]
        drops = [row["row"] for row in fixture["drop_rules"]]
        closed = [row["row"] for row in fixture["closed_episode_rules"]]
        for rows in (edges, drops, closed):
            assert len(rows) == len(set(rows)), "a plan row is transcribed twice"
        # A drop is never also an edge. Only rows 13 and 16 (record a companion
        # fact in place) have both a non-terminal and a CLOSED half.
        assert not set(drops) & (set(edges) | set(closed))
        assert set(edges) & set(closed) == {"13", "16"}
        assert set(edges) | set(drops) | set(closed) == _PLAN_ROWS

    def test_every_row_names_the_revision_change_it_comes_from(self) -> None:
        fixture = _fixture()
        named = {
            change
            for section in ("transitions", "drop_rules", "closed_episode_rules")
            for row in fixture[section]
            for change in row["change"]
        }
        outbox = {rule["change"] for rule in fixture["outbox_rules"]}
        # F6 and F8 are dispatcher rules only; every other change names a row.
        assert _REVISION_CHANGES - named == {"F6", "F8"}
        assert named | outbox == _REVISION_CHANGES

    def test_no_row_leaves_a_terminal_state_inside_the_fsm(self) -> None:
        # The rows out of CLOSED (F10, G5, R4) are applied by the orchestrator
        # as a new episode, never as core FSM edges.
        fixture = _fixture()
        terminal = {s["name"] for s in fixture["states"] if s["terminal"]}
        for row in fixture["transitions"]:
            assert not set(_sources(fixture, row)) & terminal, row["row"]
        closed_targets = {row["to"] for row in fixture["closed_episode_rules"]}
        assert closed_targets == {"CLOSED", "OBSERVED", "MERGED"}

    def test_parked_has_no_completion_bound_edge(self) -> None:
        # R2a: a draft or held PR is an intentional wait, not a stall.
        edges = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
        bound = {
            frm for frm, trigger, _to in edges if trigger == "completion_bound_expired"
        }
        non_terminal = {m.value for m in EnumPrLandingState if not m.is_terminal}
        assert bound == non_terminal - {EnumPrLandingState.PARKED.value}

    def test_draft_or_hold_parks_every_waiting_state_and_armed(self) -> None:
        # F3 plus ARMED: a draft or hold at the row's head moves each of these
        # to PARKED; NEEDS_AGENT disarms in place.
        edges = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
        for trigger in ("converted_to_draft", "hold_applied"):
            for state in (
                "COMPANION_PENDING",
                "COMPANION_OPEN",
                "CHECKS_PENDING",
                "READY",
                "ARMED",
            ):
                assert (state, trigger, "PARKED") in edges, (state, trigger)
            assert ("NEEDS_AGENT", trigger, "NEEDS_AGENT") in edges

    def test_change_control_open_has_both_rows(self) -> None:
        # R3: no verdict of the classifier seam is left without a row.
        edges = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
        assert (
            "CHECKS_PENDING",
            "verdict_change_control_open_companion_merged",
            "CHECKS_PENDING",
        ) in edges
        assert (
            "CHECKS_PENDING",
            "verdict_change_control_open_companion_not_merged",
            "OBSERVED",
        ) in edges
        assert not any("change_control_open" in r for r in _fixture()["open_rows"])

    def test_a_new_head_disarms_if_armed(self) -> None:
        # R4: the pushed row and the new-head rule out of CLOSED both disarm.
        fixture = _fixture()
        pushed = next(r for r in fixture["transitions"] if r["row"] == "1")
        assert [i["kind"] for i in pushed["intents"]] == ["github.disarm"]
        reopen = next(r for r in fixture["closed_episode_rules"] if r["row"] == "39")
        assert [i["kind"] for i in reopen["intents"]] == ["github.disarm"]

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
        # state is an intentional wait (PARKED: a draft or held PR, R2a); a
        # terminal state has no bound because it never expires. NEEDS_AGENT is
        # bounded (R2a): an expiry re-pages, deduplicated per (head, reason).
        expected: dict[EnumPrLandingState, int | None] = {
            EnumPrLandingState.OBSERVED: 300_000,
            EnumPrLandingState.PARKED: None,
            EnumPrLandingState.COMPANION_PENDING: 1_800_000,
            EnumPrLandingState.COMPANION_OPEN: 7_200_000,
            EnumPrLandingState.CHECKS_PENDING: 7_200_000,
            EnumPrLandingState.READY: 900_000,
            EnumPrLandingState.ARMED: 14_400_000,
            EnumPrLandingState.NEEDS_AGENT: 86_400_000,
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
        # No edge leaves a terminal state; the rows out of CLOSED start a new
        # episode outside the core FSM (fixture closed_episode_rules).
        sources = {
            EnumPrLandingState(t["from_state"]) for t in state_machine["transitions"]
        }
        assert EnumPrLandingState.MERGED not in sources
        assert EnumPrLandingState.CLOSED not in sources

    def test_every_non_terminal_state_can_reach_needs_agent_and_a_terminal(
        self,
    ) -> None:
        # Rows 37, 38 and 42 cover every non-terminal state, so no state is a
        # trap: each can be merged or closed, and each but PARKED (R2a) can be
        # handed to an agent on a stall.
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
            if state is not EnumPrLandingState.PARKED:
                assert EnumPrLandingState.NEEDS_AGENT.value in targets, state
            assert EnumPrLandingState.MERGED.value in targets, state
            assert EnumPrLandingState.CLOSED.value in targets, state

    def test_every_fixture_intent_names_a_model_member(self) -> None:
        kinds = {
            member.value
            for member in EnumPrLandingIntentKind
            if member not in ORCHESTRATOR_ISSUED_INTENT_KINDS
        }
        reasons = {member.value for member in EnumPrLandingAgentReason}
        for row_id, intent in _all_intents(_fixture()):
            assert intent["kind"] in kinds, row_id
            if intent["kind"] == EnumPrLandingIntentKind.AGENT_NEEDED.value:
                assert intent["reason"] in reasons, row_id
        used = {intent["kind"] for _row_id, intent in _all_intents(_fixture())}
        assert used == kinds, f"intent kinds no row uses: {sorted(kinds - used)}"

    def test_every_agent_reason_is_raised_by_some_row(self) -> None:
        raised = {
            intent["reason"]
            for _row_id, intent in _all_intents(_fixture())
            if intent["kind"] == EnumPrLandingIntentKind.AGENT_NEEDED.value
        }
        assert raised == {member.value for member in EnumPrLandingAgentReason}

    def test_the_contract_keys_state_io_by_landing_key(self) -> None:
        state_io = _contract(_ORCHESTRATOR)["state_io"]
        assert state_io["table"] == "pr_landing_workflow_state"
        assert state_io["key"] == "landing_key"
        assert "landing_key" in ModelPrLandingState.model_fields
        assert "landing_key" in ModelPrLandingObservation.model_fields


def _cases() -> list[dict[str, Any]]:
    loaded = _load_yaml(_COUNTEREXAMPLES)["cases"]
    assert isinstance(loaded, list)
    return cast("list[dict[str, Any]]", loaded)


def _reducer_cases() -> list[dict[str, Any]]:
    return [c for c in _cases() if c.get("scope") != "orchestrator"]


def _walk(case: dict[str, Any]) -> list[str]:
    """Replay one counterexample case on the frozen table; return the states."""
    fixture = _fixture()
    edges = _contract_edges(_contract(_ORCHESTRATOR)["state_machine"])
    intents_by_edge = _row_intents(fixture)
    closed_rules = {r["row"]: r for r in fixture["closed_episode_rules"]}
    drops = {r["row"] for r in fixture["drop_rules"]} | {"head_match", "terminal"}
    terminal = {s["name"] for s in fixture["states"] if s["terminal"]}
    kinds = {m.value for m in EnumPrLandingObservationKind} | {"evaluation"}

    state = case["start"]["state"]
    visited = [state]
    for step in case["steps"]:
        assert step["obs"]["kind"] in kinds, (case["id"], step)
        expect = step["expect"]
        outcomes = {"trigger", "closed_rule", "drop"} & set(expect)
        assert len(outcomes) == 1, (case["id"], expect)
        emitted = set(expect.get("intents", []))
        if "trigger" in expect:
            edge = (state, expect["trigger"], expect["to"])
            assert edge in edges, (case["id"], edge)
            assert emitted <= intents_by_edge[(state, expect["trigger"])], (
                case["id"],
                edge,
            )
            state = expect["to"]
        elif "closed_rule" in expect:
            assert state == EnumPrLandingState.CLOSED.value, case["id"]
            rule = closed_rules[expect["closed_rule"]]
            assert rule["to"] == expect["to"], case["id"]
            assert emitted <= {i["kind"] for i in rule["intents"]}, case["id"]
            state = expect["to"]
        else:
            assert expect["drop"] in drops, (case["id"], expect["drop"])
            assert not emitted, case["id"]
            if expect["drop"] == "terminal":
                assert state in terminal, case["id"]
        visited.append(state)
    return visited


class TestAc1Counterexamples:
    """Each model counterexample, replayed on the revised table, is closed."""

    def test_every_revision_change_has_a_case(self) -> None:
        assert {c["change"] for c in _cases()} == _REVISION_CHANGES
        ids = [c["id"] for c in _cases()]
        assert len(ids) == len(set(ids))

    def test_the_three_disarm_cases_and_closed_are_covered(self) -> None:
        # The design review's disarm cases: an already armed PR, an unsent
        # old-head arm and an arm in flight, plus the new head out of CLOSED
        # that the reopen run found.
        cases = {c.get("disarm_case") for c in _cases() if c["change"] == "R4"}
        assert cases == _DISARM_CASES
        for case in _cases():
            if case["change"] != "R4":
                continue
            first = case["steps"][0]["expect"]
            assert first.get("intents") == ["github.disarm"], case["id"]

    def test_an_orchestrator_case_states_its_check(self) -> None:
        for case in _cases():
            if case.get("scope") == "orchestrator":
                assert case["check"], case["id"]
                assert "steps" not in case, case["id"]
            else:
                assert case["steps"], case["id"]

    @pytest.mark.parametrize("case", _reducer_cases(), ids=lambda c: c["id"])
    def test_the_case_replays_on_the_frozen_table(self, case: dict[str, Any]) -> None:
        visited = _walk(case)
        assert len(visited) == len(case["steps"]) + 1

    def test_a_case_that_leaves_an_old_row_is_refused(self) -> None:
        # Positive control: the plan-as-written edge the R3 counterexample took
        # (CHECKS_PENDING back to COMPANION_OPEN) is not in the revised table.
        case = copy.deepcopy(
            next(
                c
                for c in _cases()
                if c["id"].startswith("R3-") and "not-merged" not in c["id"]
            )
        )
        case["steps"][0]["expect"] = {
            "trigger": "verdict_change_control_open_companion_merged",
            "to": "COMPANION_OPEN",
        }
        with pytest.raises(AssertionError):
            _walk(case)


class TestAc1RevisionModels:
    """The row, observation and intent carry the revision's new fields."""

    def _row(self, **fields: Any) -> ModelPrLandingState:
        base: dict[str, Any] = {
            "repository": "OmniNode-ai/omnimarket",
            "pr_number": 1,
            "state": EnumPrLandingState.COMPANION_PENDING,
            "seq": 0,
            "entered_state_at": "2026-09-27T00:00:00+00:00",
        }
        return ModelPrLandingState.model_validate({**base, **fields})

    def test_the_row_carries_the_revision_fields(self) -> None:
        for field in (
            "source_seq",
            "episode",
            "state_entry_generation",
            "expected_attempts",
            "outbox",
        ):
            assert field in ModelPrLandingState.model_fields, field

    def test_pending_exactly_while_a_command_is_in_flight(self) -> None:
        # CompTracked (R1) at the model boundary.
        row = self._row(companion={"status": "pending", "command_id": "c1"})
        assert row.companion.command_id == "c1"
        with pytest.raises(ValidationError, match="exactly while"):
            self._row(companion={"status": "pending"})
        with pytest.raises(ValidationError, match="exactly while"):
            self._row(companion={"status": "open", "occ_pr": 7, "command_id": "c1"})

    def test_a_bound_expiry_names_its_state_entry(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        fields = ModelPrLandingObservation.from_ingress(topic, payload).model_dump()
        expiry = {**fields, "kind": "bound_expired", "episode": 0}
        with pytest.raises(ValidationError, match="bound_expired"):
            ModelPrLandingObservation.model_validate(expiry)
        observation = ModelPrLandingObservation.model_validate(
            {**expiry, "state_entry_generation": 3}
        )
        assert observation.state_entry_generation == 3

    def test_a_companion_outcome_names_its_command(self) -> None:
        topic, payload = _recorded(next(_ingress_fixtures()))
        fields = ModelPrLandingObservation.from_ingress(topic, payload).model_dump()
        with pytest.raises(ValidationError, match="command_id"):
            ModelPrLandingObservation.model_validate(
                {**fields, "kind": "companion_outcome"}
            )
        with pytest.raises(ValidationError, match="command_id"):
            ModelPrLandingObservation.model_validate({**fields, "command_id": "c1"})

    def test_an_arm_of_the_product_pr_carries_its_expected_head(self) -> None:
        with pytest.raises(ValidationError, match="expected head"):
            ModelPrLandingIntent(
                kind=EnumPrLandingIntentKind.GITHUB_ARM,
                repository="OmniNode-ai/omnimarket",
                pr_number=1,
            )
        ModelPrLandingIntent(
            kind=EnumPrLandingIntentKind.GITHUB_ARM,
            repository="OmniNode-ai/omnimarket",
            pr_number=1,
            target_pr=2,
        )

    def test_a_companion_command_carries_its_id(self) -> None:
        with pytest.raises(ValidationError, match="command_id"):
            ModelPrLandingIntent(
                kind=EnumPrLandingIntentKind.COMPANION_REGENERATE,
                repository="OmniNode-ai/omnimarket",
                pr_number=1,
            )

    def test_terminals_are_keyed_by_episode(self) -> None:
        assert "episode" in ModelPrLandingMerged.model_fields
        assert "episode" in ModelPrLandingClosed.model_fields


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
            ModelPrLandingObservation.model_validate({**fields, "kind": "not_a_kind"})

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
            command_id="derive-1",
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
    """Discovery sees both nodes; auto-wiring subscribes the reducer on no profile.

    The orchestrator is wired since the wave-3 compose (OMN-19829): its
    wiring is asserted by
    tests/unit/nodes/node_pr_landing_orchestrator/test_pr_landing_contract_wiring.py
    and driven over the runtime's discovery and routing in the seam test. The
    reducer is called in process by the orchestrator and stays unwired.
    """

    def test_both_nodes_are_registered_for_runtime_discovery(self) -> None:
        pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text("utf-8"))
        entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
        for node in _NEW_NODES:
            assert entry_points[node] == f"omnimarket.nodes.{node}"

    def test_runtime_discovery_wires_no_subscription_for_the_reducer(self) -> None:
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
                if contract.name != _REDUCER:
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
        # Not vacuous: the reducer is owned by a consumer profile and was prepared.
        assert owned_somewhere == {_REDUCER}

    def test_the_reducer_contract_declares_no_bus_surface(self) -> None:
        raw = _contract(_REDUCER)
        for key in ("handler_routing", "handler", "event_bus", "published_events"):
            assert key not in raw, f"{_REDUCER} declares {key}; it is called in process"


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
    # As build_graph does: externally_produced_topics name the non-contract
    # publisher of a consumed topic (the pr-merged publisher workflow).
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
    def test_exactly_the_orchestrator_publishes_the_topic(self, topic: str) -> None:
        # Since the wave-3 compose (OMN-19829) the orchestrator declares its
        # publications, in the same commit as the projection's subscriptions.
        # The next two tests prove the graph closes against the gate's own
        # defect finder instead of asserting it.
        assert _publishers_of(topic) == [_ORCHESTRATOR]

    def test_the_graph_gate_passes_the_wired_orchestrator(self, tmp_path: Path) -> None:
        findings = _owned_findings(tmp_path, _contract(_ORCHESTRATOR))
        assert findings == []

    def test_the_graph_gate_refuses_an_unrouted_or_unconsumed_declaration(
        self, tmp_path: Path
    ) -> None:
        # Positive control: the same finder over the same census sees a
        # subscription with no handler_routing (DECLARED_BUT_UNWIRED) and a
        # publication nobody consumes (ORPHANED_PRODUCER).
        raw = _contract(_ORCHESTRATOR)
        del raw["handler_routing"]
        unconsumed = "onex.evt.omnimarket.pr-landing-unconsumed-control.v1"
        raw["event_bus"]["publish_topics"].append(unconsumed)
        findings = _owned_findings(tmp_path, raw)
        orphaned = {f.topic for f in findings if f.defect == "ORPHANED_PRODUCER"}
        assert orphaned == {unconsumed}
        assert any(f.defect == "DECLARED_BUT_UNWIRED" for f in findings)

    def test_the_bus_seam_in_the_fixture_names_the_owned_topics(self) -> None:
        seam = _fixture()["bus_seam"]
        assert sorted(seam["owned_by_this_task"]) == sorted(_OWNED_TOPICS.values())
        assert set(seam["owned_by_this_task"]) <= set(seam["publish"])
        assert topics.OCC_AUTOBIND_COMMAND_TOPIC_V1 in seam["subscribe"]
        assert topics.PR_MERGED_TOPIC_V1 in seam["subscribe"]
