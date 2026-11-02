# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The strict-selection audit (AC5) and the labelled-proposition coverage check (AC1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_compaction_ground_truth_compute.handlers.handler_ground_truth_audit import (
    HandlerCompactionGroundTruthAudit,
)
from omnimarket.nodes.node_compaction_ground_truth_compute.models import (
    EnumGroundTruthKind,
    ModelGroundTruthAuditRequest,
    ModelGroundTruthAuditResult,
    ModelKindExpectation,
    ModelLabelledProposition,
)
from tests.nodes.node_compaction_ground_truth_compute import fixture_ledger as f
from tests.nodes.node_compaction_ground_truth_compute.test_ground_truth import derive

pytestmark = pytest.mark.unit
K = EnumGroundTruthKind
LABELS = Path(__file__).resolve().parents[2] / "fixtures/context_compaction/labels.yaml"

AUDITED_T1 = (
    ModelKindExpectation(
        kind=K.RULING,
        positives=(f.R1, f.R2, f.R4, f.R5, f.CORR5),
        negatives=(f.PRE_RULING, f.R3, f.R6),
    ),
    ModelKindExpectation(kind=K.OPERATOR_CONSENT, positives=(f.C1,), negatives=(f.C2,)),
    ModelKindExpectation(
        kind=K.OPEN_CLAIM,
        positives=(f.CLAIM_OLD, f.CLAIM_Y),
        negatives=(f.CLAIM_X, f.CLAIM_OTHER),
    ),
    ModelKindExpectation(kind=K.HOLD, positives=(f.H1, f.H2, f.H3), negatives=(f.H4,)),
    ModelKindExpectation(
        kind=K.OPEN_ASK, positives=(f.M1, f.M2, f.M3), negatives=(f.M4,)
    ),
    ModelKindExpectation(kind=K.PROMISE_SENT, positives=(f.M4,), negatives=(f.M8,)),
    ModelKindExpectation(
        kind=K.DRAFT_UNSENT, positives=(f.DRAFT1, f.DRAFT2), negatives=()
    ),
    ModelKindExpectation(
        kind=K.UNACKED_MSG, positives=(f.M5, f.M6), negatives=(f.M7, f.M9)
    ),
)


def audit(
    now: str = f.T1,
    expectations: tuple[ModelKindExpectation, ...] = AUDITED_T1,
    propositions: tuple[ModelLabelledProposition, ...] = (),
) -> ModelGroundTruthAuditResult:
    return HandlerCompactionGroundTruthAudit().handle(
        ModelGroundTruthAuditRequest(
            result=derive(now), expectations=expectations, propositions=propositions
        )
    )


def test_audited_set_passes() -> None:
    result = audit()
    assert result.passed, result
    assert result.missing_positives == ()
    assert result.present_negatives == ()
    assert result.unexpected_items == ()
    assert result.size_equals_prefix is False


def test_a_negative_in_the_set_fails() -> None:
    bad = tuple(
        e.model_copy(update={"negatives": (*e.negatives, f.H1)})
        if e.kind is K.HOLD
        else e
        for e in AUDITED_T1
    )
    result = audit(expectations=bad)
    assert not result.passed
    assert [(x.kind, x.stamp) for x in result.present_negatives] == [(K.HOLD, f.H1)]


def test_a_positive_missing_fails() -> None:
    bad = tuple(
        e.model_copy(update={"positives": (*e.positives, f.TERM_X)})
        if e.kind is K.OPEN_CLAIM
        else e
        for e in AUDITED_T1
    )
    result = audit(expectations=bad)
    assert not result.passed
    assert [(x.kind, x.stamp) for x in result.missing_positives] == [
        (K.OPEN_CLAIM, f.TERM_X)
    ]


def test_an_item_outside_the_positives_fails() -> None:
    short = tuple(e for e in AUDITED_T1 if e.kind is not K.UNACKED_MSG)
    result = audit(expectations=short)
    assert not result.passed
    assert {x.kind for x in result.unexpected_items} == {K.UNACKED_MSG}


def test_a_set_the_size_of_the_prefix_fails() -> None:
    derived = derive(f.T1)
    padded = derived.model_copy(
        update={
            "items": derived.items
            * (derived.rows_as_of_count // len(derived.items) + 1)
        }
    )
    padded = padded.model_copy(
        update={"items": padded.items[: derived.rows_as_of_count]}
    )
    result = HandlerCompactionGroundTruthAudit().handle(
        ModelGroundTruthAuditRequest(result=padded, expectations=AUDITED_T1)
    )
    assert result.size_equals_prefix is True
    assert not result.passed


def test_labelled_propositions_with_a_covering_row_must_be_derived() -> None:
    covered = ModelLabelledProposition(
        case_id="C", boundary=1, proposition_id="p1", covered_by=f"RULING {f.R4}"
    )
    uncovered = ModelLabelledProposition(
        case_id="C", boundary=1, proposition_id="p2", covered_by=None
    )
    lost = ModelLabelledProposition(
        case_id="C", boundary=1, proposition_id="p3", covered_by=f"RULING {f.R3}"
    )
    result = audit(propositions=(covered, uncovered, lost))
    assert [x.proposition_id for x in result.uncovered_propositions] == ["p3"]
    assert not result.passed


def test_the_row_type_of_the_covering_cell_must_match() -> None:
    wrong_type = ModelLabelledProposition(
        case_id="C", boundary=1, proposition_id="p1", covered_by=f"HOLD {f.R4}"
    )
    result = audit(propositions=(wrong_type,))
    assert [x.proposition_id for x in result.uncovered_propositions] == ["p1"]


def test_labels_yaml_coverage_cells_map_onto_the_proposition_model() -> None:
    """The TE.1 file's ``ledger_coverage`` cells load into the audit's input model."""
    raw = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    propositions = [
        ModelLabelledProposition(
            case_id=case["case_id"],
            boundary=cover["boundary"],
            proposition_id=prop["label_id"],
            covered_by=cover["covered_by"],
        )
        for case in raw["cases"]
        for prop in case["propositions"]
        for cover in prop.get("ledger_coverage", ())
    ]
    covered = [p for p in propositions if p.covered_by]
    assert len(propositions) > len(covered) > 0
    assert all(p.covered_by and p.covered_by.split()[0].isupper() for p in covered)
