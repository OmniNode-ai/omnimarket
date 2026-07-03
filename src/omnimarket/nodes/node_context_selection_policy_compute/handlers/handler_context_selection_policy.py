# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Context-selection policy handler — pure, deterministic, reason-annotated.

Implements the Context Authority Rule (OMN-12843 / M3): each selected context
factor is stamped with the Authority 5-tuple {factor, source, selection_reason,
effectiveness_score, experiment_cohort}. Selection ranks candidates by measured
effectiveness resolved from the M2 capsule store (passed in by the caller — this
handler does NO I/O), with deterministic tie-breaking by stable source id.

Policy (mirrors the routing-policy precedent, applied to context):

* Experiment arm (forced/arm-fixed) candidates are admitted ONLY when an active
  experiment cohort is present and matches; they are stamped
  ``EXPERIMENT_ASSIGNMENT`` and carry the cohort id. A forced candidate without
  a matching active cohort is a hard validation error.
* A required candidate (profile-declared) is admitted and stamped
  ``POLICY_REQUIRED_FACTOR``. If it has no measured score it falls to the
  explicit ``FALLBACK_NO_SCORE`` reason — never a silent default.
* A scored candidate is admitted and stamped ``POLICY_EFFECTIVENESS``.
* A candidate justified ONLY by profile declaration with no effectiveness score
  AND no experiment cohort is the hidden-authority case and is REJECTED.

No randomness, no I/O, same input = same output.
"""

from __future__ import annotations

from omnibase_core.enums.enum_selection_reason import EnumSelectionReason

from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_request import (
    ModelContextCandidate,
    ModelContextSelectionRequest,
)
from omnimarket.nodes.node_context_selection_policy_compute.models.model_selection_result import (
    EnumSelectionStatus,
    ModelContextSelectionResult,
    ModelFactorSelection,
)


class ContextSelectionPolicyError(ValueError):
    """Raised when a candidate violates the Context Authority Rule.

    Covers the hidden-authority case (no score, no cohort, not required) and the
    experiment-arm-without-cohort case.
    """


def _classify(
    candidate: ModelContextCandidate,
    *,
    active_cohort: str | None,
) -> tuple[EnumSelectionReason, str | None]:
    """Resolve the authority reason (and cohort) for a candidate or reject it."""
    # Experiment arm: forced/arm-fixed candidates require a matching active cohort.
    if candidate.forced_experiment_cohort is not None:
        if active_cohort is None:
            raise ContextSelectionPolicyError(
                f"Candidate {candidate.source!r} is arm-fixed to cohort "
                f"{candidate.forced_experiment_cohort!r} but no active experiment "
                "cohort is present; a forced injection with no cohort is forbidden."
            )
        if candidate.forced_experiment_cohort != active_cohort:
            raise ContextSelectionPolicyError(
                f"Candidate {candidate.source!r} is arm-fixed to cohort "
                f"{candidate.forced_experiment_cohort!r} which does not match the "
                f"active experiment cohort {active_cohort!r}."
            )
        if candidate.effectiveness_score is None:
            raise ContextSelectionPolicyError(
                f"Experiment-arm candidate {candidate.source!r} must carry a "
                "measured effectiveness_score; only FALLBACK_NO_SCORE selections "
                "may have a null score."
            )
        return EnumSelectionReason.EXPERIMENT_ASSIGNMENT, active_cohort

    # Scored candidate: ranked in by measured effectiveness.
    if candidate.effectiveness_score is not None:
        return EnumSelectionReason.POLICY_EFFECTIVENESS, None

    # Required-but-unscored: admitted, explicitly stamped as no-score fallback.
    if candidate.is_required:
        return EnumSelectionReason.FALLBACK_NO_SCORE, None

    # Hidden authority: no score, no cohort, not required.
    raise ContextSelectionPolicyError(
        f"Candidate {candidate.source!r} (factor {candidate.factor.value!r}) is "
        "justified only by profile declaration with no effectiveness score and no "
        "experiment cohort; hidden authority is forbidden by the Context Authority "
        "Rule."
    )


def _rank_key(candidate: ModelContextCandidate) -> tuple[int, float, str]:
    """Deterministic ordering key.

    Sort primary by effectiveness score (descending), with unscored candidates
    last; ties broken deterministically by stable source id (ascending). The
    leading bucket flag keeps scored candidates ahead of unscored ones
    regardless of score value.
    """
    score = candidate.effectiveness_score
    if score is None:
        # Unscored candidates sort into the trailing bucket; score is irrelevant.
        return (1, 0.0, candidate.source)
    # bucket 0 keeps scored candidates ahead; negate for descending order.
    return (0, -score, candidate.source)


class HandlerContextSelectionPolicy:
    """Select context factors by policy + measured effectiveness, with authority."""

    def handle(
        self, request: ModelContextSelectionRequest
    ) -> ModelContextSelectionResult:
        ordered = sorted(request.candidates, key=_rank_key)

        selections: list[ModelFactorSelection] = []
        for index, candidate in enumerate(ordered):
            reason, cohort = _classify(
                candidate, active_cohort=request.active_experiment_cohort
            )
            effectiveness_score = (
                None
                if reason == EnumSelectionReason.FALLBACK_NO_SCORE
                else candidate.effectiveness_score
            )
            selections.append(
                ModelFactorSelection(
                    factor=candidate.factor,
                    source=candidate.source,
                    selection_reason=reason,
                    effectiveness_score=effectiveness_score,
                    experiment_cohort=cohort,
                    rank=index + 1,
                )
            )

        return ModelContextSelectionResult(
            status=EnumSelectionStatus.OK,
            selections=tuple(selections),
            request_id=request.request_id,
        )


__all__ = [
    "ContextSelectionPolicyError",
    "HandlerContextSelectionPolicy",
]
