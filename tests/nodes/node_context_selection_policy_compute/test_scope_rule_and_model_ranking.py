# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TC.2 selection-policy extensions for node_context_selection_policy_compute.

Covers the ``POLICY_SCOPE_RULE`` path (a candidate admitted by a rule its
profile declares carries a ``rule_ref`` and a null score), the extended
authority invariant on ``ModelFactorSelection``, a model's ranking of optional
candidates (accepted only as a subset of the eligible optional ids, otherwise
ignored and counted in ``selection_rejected``), fill by rank then restore
precedence order, and selection that is independent of candidate input order.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason
from pydantic import ValidationError

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    ContextSelectionPolicyError,
    HandlerContextSelectionPolicy,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_request import (
    ModelContextCandidate,
    ModelContextSelectionRequest,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_result import (
    EnumSelectionStatus,
    ModelContextSelectionResult,
    ModelFactorSelection,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_rule_ref import (
    ModelSelectionRuleRef,
)

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_context_selection_policy_compute/contract.yaml"
)
_COMPLETED_TOPIC = "onex.evt.omnimarket.context-selection-policy-completed.v1"  # onex-topic-allow: this node's declared publish topic

_RULE = ModelSelectionRuleRef(profile_id="lane_brief", rule_id="scope-invariants")


def _candidate(
    *,
    source: str,
    factor: EnumContextFactor = EnumContextFactor.EXEMPLAR,
    effectiveness_score: float | None = None,
    is_required: bool = False,
    scope_rule_ref: ModelSelectionRuleRef | None = None,
) -> ModelContextCandidate:
    return ModelContextCandidate(
        factor=factor,
        source=source,
        effectiveness_score=effectiveness_score,
        is_required=is_required,
        scope_rule_ref=scope_rule_ref,
    )


def _selection(
    *,
    reason: EnumSelectionReason,
    score: float | None,
    rule_ref: ModelSelectionRuleRef | None,
) -> ModelFactorSelection:
    return ModelFactorSelection(
        factor=EnumContextFactor.EXEMPLAR,
        source="capsule-a",
        selection_reason=reason,
        effectiveness_score=score,
        rule_ref=rule_ref,
        rank=1,
    )


# --- ModelFactorSelection authority invariant (v6) -------------------------


def test_scope_rule_selection_with_rule_ref_and_null_score_constructs() -> None:
    selection = _selection(
        reason=EnumSelectionReason.POLICY_SCOPE_RULE, score=None, rule_ref=_RULE
    )
    assert selection.rule_ref == _RULE
    assert selection.effectiveness_score is None


def test_scope_rule_selection_without_rule_ref_is_refused() -> None:
    with pytest.raises(ValidationError, match="rule_ref"):
        _selection(
            reason=EnumSelectionReason.POLICY_SCOPE_RULE, score=None, rule_ref=None
        )


def test_scope_rule_selection_with_score_is_refused() -> None:
    with pytest.raises(ValidationError, match="effectiveness_score"):
        _selection(
            reason=EnumSelectionReason.POLICY_SCOPE_RULE, score=0.7, rule_ref=_RULE
        )


@pytest.mark.parametrize(
    ("reason", "score"),
    [
        (EnumSelectionReason.POLICY_EFFECTIVENESS, 0.5),
        (EnumSelectionReason.POLICY_REQUIRED_FACTOR, 0.5),
        (EnumSelectionReason.FALLBACK_NO_SCORE, None),
        (EnumSelectionReason.EXPLORATION, 0.5),
    ],
)
def test_rule_ref_on_any_other_reason_is_refused(
    reason: EnumSelectionReason, score: float | None
) -> None:
    with pytest.raises(ValidationError, match="rule_ref"):
        _selection(reason=reason, score=score, rule_ref=_RULE)


def test_fallback_no_score_unchanged() -> None:
    selection = _selection(
        reason=EnumSelectionReason.FALLBACK_NO_SCORE, score=None, rule_ref=None
    )
    assert selection.rule_ref is None
    with pytest.raises(ValidationError, match="FALLBACK_NO_SCORE"):
        _selection(
            reason=EnumSelectionReason.FALLBACK_NO_SCORE, score=0.4, rule_ref=None
        )


def test_candidate_refuses_rule_ref_beside_a_score() -> None:
    with pytest.raises(ValidationError, match="scope_rule_ref"):
        _candidate(source="capsule-a", effectiveness_score=0.6, scope_rule_ref=_RULE)


# --- POLICY_SCOPE_RULE stamping -------------------------------------------


@pytest.mark.parametrize("is_required", [True, False])
def test_rule_admitted_candidate_is_stamped_policy_scope_rule(
    is_required: bool,
) -> None:
    request = ModelContextSelectionRequest(
        candidates=(
            _candidate(
                source="invariant-7",
                factor=EnumContextFactor.ARCHITECTURE_PATTERNS,
                is_required=is_required,
                scope_rule_ref=_RULE,
            ),
        ),
    )
    result = HandlerContextSelectionPolicy().handle(request)
    (selection,) = result.selections
    assert selection.selection_reason == EnumSelectionReason.POLICY_SCOPE_RULE
    assert selection.rule_ref == _RULE
    assert selection.effectiveness_score is None


def test_no_rule_no_score_no_cohort_is_still_hidden_authority() -> None:
    request = ModelContextSelectionRequest(
        candidates=(_candidate(source="hidden-claude-md"),),
    )
    with pytest.raises(ContextSelectionPolicyError, match="hidden authority"):
        HandlerContextSelectionPolicy().handle(request)


# --- model ranking, fill by rank, restore precedence -----------------------


def _ranked_pool() -> tuple[ModelContextCandidate, ...]:
    return (
        _candidate(source="req", effectiveness_score=0.1, is_required=True),
        _candidate(source="opt-a", effectiveness_score=0.9),
        _candidate(source="opt-b", effectiveness_score=0.8),
        _candidate(source="opt-c", effectiveness_score=0.7),
    )


def test_valid_model_ranking_fills_by_rank_then_restores_precedence() -> None:
    request = ModelContextSelectionRequest(
        candidates=_ranked_pool(),
        model_ranking=("opt-c", "opt-b"),
        max_optional=2,
    )
    result = HandlerContextSelectionPolicy().handle(request)
    assert [s.source for s in result.selections] == ["opt-b", "opt-c", "req"]
    assert [s.rank for s in result.selections] == [1, 2, 3]
    assert result.selection_rejected == 0
    assert result.selection_rejected_ids == ()


def test_ranking_naming_an_outside_id_is_ignored_and_counted() -> None:
    request = ModelContextSelectionRequest(
        candidates=_ranked_pool(),
        model_ranking=("opt-c", "not-a-candidate"),
        max_optional=1,
    )
    result = HandlerContextSelectionPolicy().handle(request)
    # Deterministic order fills the single optional slot: opt-a (0.9).
    assert [s.source for s in result.selections] == ["opt-a", "req"]
    assert result.selection_rejected == 1
    assert result.selection_rejected_ids == ("not-a-candidate",)


def test_ranking_naming_a_required_id_is_ignored_and_counted() -> None:
    request = ModelContextSelectionRequest(
        candidates=_ranked_pool(),
        model_ranking=("req", "opt-c"),
        max_optional=1,
    )
    result = HandlerContextSelectionPolicy().handle(request)
    assert [s.source for s in result.selections] == ["opt-a", "req"]
    assert result.selection_rejected == 1
    assert result.selection_rejected_ids == ("req",)


def test_ranking_never_drops_a_required_candidate() -> None:
    request = ModelContextSelectionRequest(
        candidates=_ranked_pool(),
        model_ranking=("opt-a",),
        max_optional=0,
    )
    result = HandlerContextSelectionPolicy().handle(request)
    assert [s.source for s in result.selections] == ["req"]
    assert result.selection_rejected == 0


# --- permutation invariance -------------------------------------------------


def test_selection_identical_under_every_input_permutation() -> None:
    pool = (
        _candidate(source="req", effectiveness_score=0.5, is_required=True),
        _candidate(source="tie", effectiveness_score=0.5),
        _candidate(
            source="tie",
            factor=EnumContextFactor.GOLDEN_CHAIN,
            effectiveness_score=0.5,
        ),
        _candidate(source="fallback", is_required=True),
        _candidate(source="scoped", scope_rule_ref=_RULE),
        _candidate(source="opt-low", effectiveness_score=0.2),
    )
    handler = HandlerContextSelectionPolicy()
    outputs = {
        handler.handle(
            ModelContextSelectionRequest(
                candidates=perm, model_ranking=("opt-low", "tie"), max_optional=2
            )
        ).model_dump_json()
        for perm in itertools.permutations(pool)
    }
    assert len(outputs) == 1


# --- declared output states ---------------------------------------------------


def test_contract_terminal_event_is_the_declared_publish_topic() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert contract["terminal_event"] == _COMPLETED_TOPIC
    assert _COMPLETED_TOPIC in contract["event_bus"]["publish_topics"]
    assert {"selection_rejected", "selection_rejected_ids"} <= set(contract["outputs"])


def test_error_status_result_carries_message_and_no_selections() -> None:
    result = ModelContextSelectionResult(
        status=EnumSelectionStatus.ERROR, error="hidden authority"
    )
    assert result.status.value == "error"
    assert result.error == "hidden authority"
    assert result.selections == ()
    assert result.selection_rejected == 0
