# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_context_selection_policy_compute (OMN-12843 / M3).

DoD (OMN-12843): every selected context factor carries the Context Authority
5-tuple {factor, source, selection_reason, effectiveness_score,
experiment_cohort}; hidden authority is rejected; experiment arms require a
cohort; ranking is by M2-resolved measured effectiveness, not a usage proxy.

This chain exercises the deterministic policy end to end with a realistic mixed
candidate set (a scored exemplar, a scored architecture capsule, a required-but-
unscored golden chain, and an arm-fixed CLAUDE_MD candidate under an active
cohort), then proves:

  1. the chain ranks scored candidates by effectiveness (highest first),
  2. every selection carries the full Authority 5-tuple with the
     score-is-None XOR FALLBACK_NO_SCORE invariant,
  3. the chain is replay-deterministic (same input -> identical output).

The node is pure and non-LLM by construction: it performs only deterministic
ranking and reason classification over passed-in scores. No model-routing /
inference seam is imported or invoked anywhere in the handler, so "no LLM call
in the trace" is a structural property, asserted here by replaying the chain for
identical results.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_selection_reason import EnumSelectionReason

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    HandlerContextSelectionPolicy,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_request import (
    ModelContextCandidate,
    ModelContextSelectionRequest,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_result import (
    EnumSelectionStatus,
)

_ACTIVE_COHORT = "cohort-treatment-a"


def _chain_request() -> ModelContextSelectionRequest:
    """A realistic mixed candidate set arriving in non-ranked order."""
    return ModelContextSelectionRequest(
        candidates=(
            # Required-but-unscored golden chain -> explicit FALLBACK_NO_SCORE.
            ModelContextCandidate(
                factor=EnumContextFactor.GOLDEN_CHAIN,
                source="capsule-gc-required",
                effectiveness_score=None,
                is_required=True,
            ),
            # Lower-scored exemplar (arrives before the higher-scored one).
            ModelContextCandidate(
                factor=EnumContextFactor.EXEMPLAR,
                source="capsule-exemplar-mid",
                effectiveness_score=0.55,
                is_required=False,
            ),
            # Arm-fixed CLAUDE_MD under the active cohort -> EXPERIMENT_ASSIGNMENT.
            ModelContextCandidate(
                factor=EnumContextFactor.CLAUDE_MD,
                source="arm-fixed-claude-md",
                effectiveness_score=0.40,
                is_required=False,
                forced_experiment_cohort=_ACTIVE_COHORT,
            ),
            # Highest-scored architecture capsule -> POLICY_EFFECTIVENESS, rank 1.
            ModelContextCandidate(
                factor=EnumContextFactor.ARCHITECTURE_PATTERNS,
                source="capsule-arch-top",
                effectiveness_score=0.92,
                is_required=False,
            ),
        ),
        active_experiment_cohort=_ACTIVE_COHORT,
        request_id="golden-chain-001",
    )


@pytest.mark.unit
class TestContextSelectionPolicyGoldenChain:
    def test_authority_annotated_ranked_chain(self) -> None:
        result = HandlerContextSelectionPolicy().handle(_chain_request())

        assert result.status == EnumSelectionStatus.OK
        assert result.request_id == "golden-chain-001"
        assert len(result.selections) == 4

        # Scored candidates rank ahead of the unscored fallback, by effectiveness.
        ordered_sources = [s.source for s in result.selections]
        assert ordered_sources == [
            "capsule-arch-top",  # 0.92
            "capsule-exemplar-mid",  # 0.55
            "arm-fixed-claude-md",  # 0.40 (arm)
            "capsule-gc-required",  # fallback, last
        ]

        by_source = {s.source: s for s in result.selections}
        # Every selection carries the full Authority 5-tuple with the invariant.
        for selection in result.selections:
            assert isinstance(selection.factor, EnumContextFactor)
            assert selection.source
            assert isinstance(selection.selection_reason, EnumSelectionReason)
            has_score = selection.effectiveness_score is not None
            is_fallback = (
                selection.selection_reason == EnumSelectionReason.FALLBACK_NO_SCORE
            )
            assert has_score != is_fallback

        assert (
            by_source["capsule-arch-top"].selection_reason
            == EnumSelectionReason.POLICY_EFFECTIVENESS
        )
        assert (
            by_source["arm-fixed-claude-md"].selection_reason
            == EnumSelectionReason.EXPERIMENT_ASSIGNMENT
        )
        assert by_source["arm-fixed-claude-md"].experiment_cohort == _ACTIVE_COHORT
        assert (
            by_source["capsule-gc-required"].selection_reason
            == EnumSelectionReason.FALLBACK_NO_SCORE
        )
        assert by_source["capsule-gc-required"].effectiveness_score is None

    def test_chain_is_replay_deterministic(self) -> None:
        handler = HandlerContextSelectionPolicy()
        request = _chain_request()
        assert handler.handle(request) == handler.handle(request)
