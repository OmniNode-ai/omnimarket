# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD suite for node_context_selection_policy_compute (OMN-12843 / M3).

Encodes the Context Authority Rule: every selected context factor carries the
Authority 5-tuple {factor, source, selection_reason, effectiveness_score,
experiment_cohort}; hidden authority (profile-declared with no score and no
cohort) is rejected; experiment arms require a cohort; ranking is by measured
effectiveness, not a usage heuristic.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason

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


def _candidate(
    *,
    factor: EnumContextFactor = EnumContextFactor.EXEMPLAR,
    source: str = "capsule-a",
    effectiveness_score: float | None = 0.8,
    is_required: bool = False,
    forced_experiment_cohort: str | None = None,
) -> ModelContextCandidate:
    return ModelContextCandidate(
        factor=factor,
        source=source,
        effectiveness_score=effectiveness_score,
        is_required=is_required,
        forced_experiment_cohort=forced_experiment_cohort,
    )


def test_rejects_hardcoded_factor() -> None:
    """Hidden-authority case: a candidate with no score, no cohort, not required."""
    request = ModelContextSelectionRequest(
        candidates=(
            _candidate(
                source="hidden-claude-md",
                effectiveness_score=None,
                is_required=False,
                forced_experiment_cohort=None,
            ),
        ),
    )
    with pytest.raises(ContextSelectionPolicyError):
        HandlerContextSelectionPolicy().handle(request)


def test_every_selected_factor_carries_authority_tuple() -> None:
    """Every output entry carries the Authority 5-tuple with the score invariant."""
    request = ModelContextSelectionRequest(
        candidates=(
            _candidate(
                factor=EnumContextFactor.EXEMPLAR,
                source="capsule-a",
                effectiveness_score=0.9,
            ),
            _candidate(
                factor=EnumContextFactor.GOLDEN_CHAIN,
                source="capsule-b",
                effectiveness_score=None,
                is_required=False,
                forced_experiment_cohort=None,
            ),
        ),
    )
    # capsule-b has no score and is not required and has no cohort -> hidden
    # authority would be rejected; make it required so it is admitted via the
    # explicit FALLBACK_NO_SCORE path.
    request = ModelContextSelectionRequest(
        candidates=(
            _candidate(
                factor=EnumContextFactor.EXEMPLAR,
                source="capsule-a",
                effectiveness_score=0.9,
            ),
            _candidate(
                factor=EnumContextFactor.GOLDEN_CHAIN,
                source="capsule-b",
                effectiveness_score=None,
                is_required=True,
            ),
        ),
    )
    result = HandlerContextSelectionPolicy().handle(request)
    assert result.status == EnumSelectionStatus.OK
    assert len(result.selections) == 2
    for selection in result.selections:
        assert isinstance(selection, ModelFactorSelection)
        assert isinstance(selection.factor, EnumContextFactor)
        assert selection.source
        assert isinstance(selection.selection_reason, EnumSelectionReason)
        # effectiveness_score is not None XOR selection_reason == FALLBACK_NO_SCORE.
        has_score = selection.effectiveness_score is not None
        is_fallback = (
            selection.selection_reason == EnumSelectionReason.FALLBACK_NO_SCORE
        )
        assert has_score != is_fallback


def test_experiment_arm_requires_cohort() -> None:
    """A forced/arm-fixed candidate with no active cohort is a hard error; with one, ok."""
    # Experiment arms carry their measured score (per the Authority invariant,
    # only FALLBACK_NO_SCORE may have a null score).
    forced = _candidate(
        factor=EnumContextFactor.CLAUDE_MD,
        source="arm-fixed-claude-md",
        effectiveness_score=0.42,
        is_required=False,
        forced_experiment_cohort="cohort-treatment-a",
    )
    # No active cohort on the request -> error.
    no_cohort_request = ModelContextSelectionRequest(
        candidates=(forced,),
        active_experiment_cohort=None,
    )
    with pytest.raises(ContextSelectionPolicyError):
        HandlerContextSelectionPolicy().handle(no_cohort_request)

    # Matching active cohort -> EXPERIMENT_ASSIGNMENT, ok.
    with_cohort_request = ModelContextSelectionRequest(
        candidates=(forced,),
        active_experiment_cohort="cohort-treatment-a",
    )
    result = HandlerContextSelectionPolicy().handle(with_cohort_request)
    assert result.status == EnumSelectionStatus.OK
    assert len(result.selections) == 1
    selection = result.selections[0]
    assert selection.selection_reason == EnumSelectionReason.EXPERIMENT_ASSIGNMENT
    assert selection.experiment_cohort == "cohort-treatment-a"


def test_ranking_uses_effectiveness_not_usage_heuristic() -> None:
    """Rank strictly by passed-in effectiveness, ignoring any usage-order proxy.

    The candidates are supplied in usage/insertion order that is the inverse of
    their effectiveness order; the result must rank by effectiveness.
    """
    request = ModelContextSelectionRequest(
        candidates=(
            # Inserted first (would win a usage/recency proxy) but lower score.
            _candidate(
                factor=EnumContextFactor.LOCAL_FAILURES,
                source="capsule-low",
                effectiveness_score=0.30,
            ),
            # Inserted second but higher measured effectiveness.
            _candidate(
                factor=EnumContextFactor.ARCHITECTURE_PATTERNS,
                source="capsule-high",
                effectiveness_score=0.95,
            ),
        ),
    )
    result = HandlerContextSelectionPolicy().handle(request)
    assert result.status == EnumSelectionStatus.OK
    assert [s.source for s in result.selections] == ["capsule-high", "capsule-low"]
    assert result.selections[0].rank == 1
    assert result.selections[1].rank == 2
    assert (
        result.selections[0].selection_reason
        == EnumSelectionReason.POLICY_EFFECTIVENESS
    )


def test_selection_round_trip() -> None:
    """Output models serialize and round-trip with the authority fields intact."""
    request = ModelContextSelectionRequest(
        candidates=(
            _candidate(
                factor=EnumContextFactor.EXEMPLAR,
                source="capsule-a",
                effectiveness_score=0.9,
            ),
            _candidate(
                factor=EnumContextFactor.CLAUDE_MD,
                source="arm-fixed",
                effectiveness_score=0.5,
                forced_experiment_cohort="cohort-x",
            ),
        ),
        active_experiment_cohort="cohort-x",
        request_id="req-roundtrip",
    )
    result = HandlerContextSelectionPolicy().handle(request)
    dumped = result.model_dump_json()
    reloaded = ModelContextSelectionResult.model_validate_json(dumped)
    assert reloaded == result
    assert reloaded.request_id == "req-roundtrip"
    # Authority fields survive the round trip for every entry.
    for selection in reloaded.selections:
        assert "selection_reason" in selection.model_dump()
        assert "effectiveness_score" in selection.model_dump()
        assert "experiment_cohort" in selection.model_dump()
        assert "source" in selection.model_dump()
        assert "factor" in selection.model_dump()
