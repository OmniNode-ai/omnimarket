# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_equivalence_reducer (OMN-21190).

The reducer owns equivalence-edge admission, rejection and revocation; every
decision advances the graph revision, and revoking an edge names the keys whose
transferred labels must be relabelled.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_review_equivalence_reducer as _node_pkg
from omnimarket.nodes.node_review_equivalence_reducer.handlers.handler_review_equivalence_reducer import (
    HandlerReviewEquivalenceReducer,
    defect_key_for,
)
from omnimarket.nodes.node_review_equivalence_reducer.models.model_review_equivalence import (
    EnumEquivalenceAction,
    EnumEquivalenceEdgeStatus,
    EnumEquivalenceOutcome,
    ModelEquivalenceCommand,
    ModelEquivalenceGraphState,
    ModelEquivalenceReducerInput,
    ModelEquivalenceReducerOutput,
    ModelMatchEvidence,
)

_CONTRACT_PATH = Path(_node_pkg.__file__).parent / "contract.yaml"

A, B, C, D, E = "occ-a", "occ-b", "occ-c", "occ-d", "occ-e"


def _evidence(similarity: float = 0.91) -> ModelMatchEvidence:
    return ModelMatchEvidence(
        similarity=similarity,
        matcher="node_finding_aggregator_compute",
        original_finding_refs=("review-1/finding-1", "review-2/finding-9"),
    )


def _command(
    action: EnumEquivalenceAction,
    a: str,
    b: str,
    *,
    evidence: ModelMatchEvidence | None = None,
    reason: str = "reviewed",
) -> ModelEquivalenceCommand:
    return ModelEquivalenceCommand(
        action=action,
        occurrence_key_a=a,
        occurrence_key_b=b,
        evidence=evidence,
        reason=reason,
        decided_by="test-lane",
    )


def _admit(a: str, b: str) -> ModelEquivalenceCommand:
    return _command(EnumEquivalenceAction.ADMIT, a, b, evidence=_evidence())


def _reject(a: str, b: str) -> ModelEquivalenceCommand:
    return _command(EnumEquivalenceAction.REJECT, a, b, evidence=_evidence(0.4))


def _revoke(a: str, b: str) -> ModelEquivalenceCommand:
    return _command(EnumEquivalenceAction.REVOKE, a, b)


def _apply(
    state: ModelEquivalenceGraphState, command: ModelEquivalenceCommand
) -> ModelEquivalenceReducerOutput:
    return HandlerReviewEquivalenceReducer().handle(
        ModelEquivalenceReducerInput(
            correlation_id=uuid4(), state=state, command=command
        )
    )


@pytest.mark.unit
class TestRevisions:
    """AC3: admit, reject and revoke each advance the graph revision."""

    def test_admit_reject_revoke_sequence_advances_the_revision_each_time(
        self,
    ) -> None:
        state = ModelEquivalenceGraphState()
        assert state.revision == 0

        admitted = _apply(state, _admit(A, B))
        assert admitted.outcome is EnumEquivalenceOutcome.ADMITTED
        assert (admitted.revision_before, admitted.revision_after) == (0, 1)
        assert admitted.state.revision == 1

        rejected = _apply(admitted.state, _reject(C, D))
        assert rejected.outcome is EnumEquivalenceOutcome.REJECTED
        assert (rejected.revision_before, rejected.revision_after) == (1, 2)
        assert rejected.state.revision == 2

        revoked = _apply(rejected.state, _revoke(A, B))
        assert revoked.outcome is EnumEquivalenceOutcome.REVOKED
        assert (revoked.revision_before, revoked.revision_after) == (2, 3)
        assert revoked.state.revision == 3

    def test_edge_statuses_follow_the_sequence(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _reject(C, D)).state
        by_pair = {(e.occurrence_key_a, e.occurrence_key_b): e for e in state.edges}
        assert by_pair[(A, B)].status is EnumEquivalenceEdgeStatus.ADMITTED
        assert by_pair[(C, D)].status is EnumEquivalenceEdgeStatus.REJECTED
        state = _apply(state, _revoke(A, B)).state
        by_pair = {(e.occurrence_key_a, e.occurrence_key_b): e for e in state.edges}
        assert by_pair[(A, B)].status is EnumEquivalenceEdgeStatus.REVOKED

    def test_input_state_is_never_mutated(self) -> None:
        state = ModelEquivalenceGraphState()
        _apply(state, _admit(A, B))
        assert state.revision == 0
        assert state.edges == ()

    def test_the_same_inputs_give_the_same_output(self) -> None:
        correlation_id = uuid4()
        request = ModelEquivalenceReducerInput(
            correlation_id=correlation_id,
            state=ModelEquivalenceGraphState(),
            command=_admit(A, B),
        )
        handler = HandlerReviewEquivalenceReducer()
        assert handler.handle(request) == handler.handle(request)


@pytest.mark.unit
class TestAdmission:
    def test_edge_is_unordered_and_keyed_by_the_pair(self) -> None:
        forward = _apply(ModelEquivalenceGraphState(), _admit(A, B))
        backward = _apply(ModelEquivalenceGraphState(), _admit(B, A))
        assert forward.edge_id == backward.edge_id
        assert forward.state.edges == backward.state.edges

    def test_edge_records_its_evidence(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        (edge,) = state.edges
        assert edge.evidence is not None
        assert edge.evidence.similarity == pytest.approx(0.91)
        assert edge.evidence.original_finding_refs == (
            "review-1/finding-1",
            "review-2/finding-9",
        )

    def test_repeat_admission_is_a_duplicate_and_does_not_advance(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        again = _apply(state, _admit(B, A))
        assert again.outcome is EnumEquivalenceOutcome.DUPLICATE
        assert again.revision_after == again.revision_before == 1
        assert again.state == state

    def test_admission_needs_evidence(self) -> None:
        with pytest.raises(ValidationError):
            _command(EnumEquivalenceAction.ADMIT, A, B)

    def test_an_edge_to_itself_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _admit(A, A)

    def test_a_rejected_pair_can_be_admitted_on_new_evidence(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _reject(A, B)).state
        result = _apply(state, _admit(A, B))
        assert result.outcome is EnumEquivalenceOutcome.ADMITTED
        assert result.revision_after == 2

    def test_a_revoked_pair_can_be_admitted_again(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _revoke(A, B)).state
        result = _apply(state, _admit(A, B))
        assert result.outcome is EnumEquivalenceOutcome.ADMITTED
        assert result.revision_after == 3
        (edge,) = result.state.edges
        assert [t.status for t in edge.history] == [
            EnumEquivalenceEdgeStatus.ADMITTED,
            EnumEquivalenceEdgeStatus.REVOKED,
            EnumEquivalenceEdgeStatus.ADMITTED,
        ]
        assert [t.revision for t in edge.history] == [1, 2, 3]


@pytest.mark.unit
class TestRejectionAndRefusal:
    def test_repeat_rejection_is_a_duplicate(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _reject(A, B)).state
        again = _apply(state, _reject(A, B))
        assert again.outcome is EnumEquivalenceOutcome.DUPLICATE
        assert again.revision_after == 1

    def test_rejecting_an_admitted_edge_is_refused_not_applied(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        result = _apply(state, _reject(A, B))
        assert result.outcome is EnumEquivalenceOutcome.REFUSED
        assert result.refusal_reason is not None
        assert result.revision_after == result.revision_before == 1
        assert result.state == state

    def test_revoking_an_unknown_edge_is_refused(self) -> None:
        result = _apply(ModelEquivalenceGraphState(), _revoke(A, B))
        assert result.outcome is EnumEquivalenceOutcome.REFUSED
        assert result.revision_after == 0

    def test_revoking_a_rejected_edge_is_refused(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _reject(A, B)).state
        result = _apply(state, _revoke(A, B))
        assert result.outcome is EnumEquivalenceOutcome.REFUSED

    def test_revoking_twice_is_refused_the_second_time(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _revoke(A, B)).state
        result = _apply(state, _revoke(A, B))
        assert result.outcome is EnumEquivalenceOutcome.REFUSED
        assert result.revision_after == 2


@pytest.mark.unit
class TestDefectKeysAndRelabelling:
    def test_defect_key_is_shared_only_along_admitted_edges(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _reject(B, C)).state
        assert defect_key_for(state, A) == defect_key_for(state, B)
        assert defect_key_for(state, C) != defect_key_for(state, A)

    def test_defect_key_is_transitive_over_a_chain(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _admit(B, C)).state
        assert defect_key_for(state, A) == defect_key_for(state, C)

    def test_an_unconnected_key_has_its_own_defect_key(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        assert defect_key_for(state, E) not in {
            defect_key_for(state, A),
            defect_key_for(state, B),
        }

    def test_admission_names_the_merged_component(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        result = _apply(state, _admit(B, C))
        assert set(result.affected_occurrence_keys) == {A, B, C}
        assert result.relabel_occurrence_keys == ()
        assigned = {d.occurrence_key: d.defect_key for d in result.defect_keys}
        assert len(set(assigned.values())) == 1

    def test_revocation_names_every_key_whose_labels_must_be_relabelled(self) -> None:
        state = _apply(ModelEquivalenceGraphState(), _admit(A, B)).state
        state = _apply(state, _admit(B, C)).state
        result = _apply(state, _revoke(B, C))
        assert set(result.relabel_occurrence_keys) == {A, B, C}
        assert result.relabel_occurrence_keys == tuple(
            sorted(result.relabel_occurrence_keys)
        )
        # after the revoke, C stands alone and A with B stay together
        assert defect_key_for(result.state, A) == defect_key_for(result.state, B)
        assert defect_key_for(result.state, C) != defect_key_for(result.state, A)

    def test_rejection_triggers_no_relabelling(self) -> None:
        result = _apply(ModelEquivalenceGraphState(), _reject(A, B))
        assert result.relabel_occurrence_keys == ()


@pytest.mark.unit
class TestContract:
    def test_contract_declares_reducer_node_and_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert contract["name"] == "node_review_equivalence_reducer"
        assert contract["node_type"] == "reducer"
        command_topic = contract["runtime_dispatch"]["command_topic"]
        terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
        assert command_topic == "onex.cmd.omnimarket.review-equivalence-decide.v1"
        assert terminal == "onex.evt.omnimarket.review-equivalence-decided.v1"
        assert command_topic in contract["event_bus"]["subscribe_topics"]
        assert terminal in contract["event_bus"]["publish_topics"]

    def test_contract_handler_resolves_to_the_handler_class(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert contract["handler"]["class"] == "HandlerReviewEquivalenceReducer"
        assert (
            contract["handler"]["module"] == HandlerReviewEquivalenceReducer.__module__
        )
