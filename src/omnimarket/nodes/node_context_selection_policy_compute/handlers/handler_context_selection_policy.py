# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Context-selection policy handler — pure, deterministic, reason-annotated.

Implements the Context Authority Rule (OMN-12843 / M3): each selected context
factor is stamped with the Authority 5-tuple {factor, source, selection_reason,
effectiveness_score, experiment_cohort}. Selection ranks candidates by measured
effectiveness resolved from the M2 capsule store (passed in by the caller — this
handler does NO I/O), with deterministic tie-breaking by stable source id and
the remaining candidate fields.

Policy (mirrors the routing-policy precedent, applied to context):

* Experiment arm (forced/arm-fixed) candidates are admitted ONLY when an active
  experiment cohort is present and matches; they are stamped
  ``EXPERIMENT_ASSIGNMENT`` and carry the cohort id. A forced candidate without
  a matching active cohort is a hard validation error.
* A required candidate (profile-declared) is always kept. With no rule or
  measured score it receives the explicit ``FALLBACK_NO_SCORE`` reason — never
  a silent default.
* A scored candidate is admitted and stamped ``POLICY_EFFECTIVENESS``.
* A profile-declared scope, status or task-kind rule admits a candidate as
  ``POLICY_SCOPE_RULE``, with its rule reference and a null effectiveness score.
* An optional candidate with no rule, score or experiment cohort is the
  hidden-authority case and is REJECTED.

All candidates are classified before selection. Required candidates are always
kept. A model ranking may order eligible optional source ids for filling the
optional limit; rankings with unknown, required-only or duplicate ids are
ignored and counted. Fill by that ranking, then restore deterministic precedence
order for the output and assign consecutive ranks.

No randomness, no I/O, same input = same output.
"""

from __future__ import annotations

from collections import Counter

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

    # Profile-declared rule: admitted without a measured score.
    if candidate.scope_rule_ref is not None:
        return EnumSelectionReason.POLICY_SCOPE_RULE, None

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


def _rank_key(
    candidate: ModelContextCandidate,
) -> tuple[int, float, str, str, bool, str, str, str]:
    """Deterministic ordering key.

    Sort primary by effectiveness score (descending), with unscored candidates
    last; ties broken deterministically by stable source id (ascending). The
    leading bucket flag keeps scored candidates ahead of unscored ones
    regardless of score value. Remaining ties use every other candidate field
    so different candidates have a total order independent of input order.
    """
    score = candidate.effectiveness_score
    rule_ref = candidate.scope_rule_ref
    return (
        1 if score is None else 0,
        0.0 if score is None else -score,
        candidate.source,
        candidate.factor.value,
        candidate.is_required,
        candidate.forced_experiment_cohort or "",
        rule_ref.profile_id if rule_ref is not None else "",
        rule_ref.rule_id if rule_ref is not None else "",
    )


class HandlerContextSelectionPolicy:
    """Select context factors by policy + measured effectiveness, with authority."""

    def handle(
        self, request: ModelContextSelectionRequest
    ) -> ModelContextSelectionResult:
        classified = [
            (
                candidate,
                *_classify(candidate, active_cohort=request.active_experiment_cohort),
            )
            for candidate in request.candidates
        ]
        ordered = sorted(classified, key=lambda entry: _rank_key(entry[0]))

        kept_indices = {
            index
            for index, (candidate, _, _) in enumerate(ordered)
            if candidate.is_required
        }
        optional_indices = [
            index
            for index, (candidate, _, _) in enumerate(ordered)
            if not candidate.is_required
        ]
        rejected_ids: tuple[str, ...] = ()
        if request.model_ranking is not None:
            eligible_ids = {ordered[index][0].source for index in optional_indices}
            counts = Counter(request.model_ranking)
            rejected_ids = tuple(
                sorted(
                    source
                    for source, count in counts.items()
                    if source not in eligible_ids or count > 1
                )
            )
            if not rejected_ids:
                positions = {
                    source: index for index, source in enumerate(request.model_ranking)
                }
                optional_indices.sort(
                    key=lambda index: positions.get(
                        ordered[index][0].source, len(positions)
                    )
                )
        kept_indices.update(optional_indices[: request.max_optional])

        selections: list[ModelFactorSelection] = []
        for index, (candidate, reason, cohort) in enumerate(ordered):
            if index not in kept_indices:
                continue
            effectiveness_score = (
                None
                if reason
                in (
                    EnumSelectionReason.FALLBACK_NO_SCORE,
                    EnumSelectionReason.POLICY_SCOPE_RULE,
                )
                else candidate.effectiveness_score
            )
            selections.append(
                ModelFactorSelection(
                    factor=candidate.factor,
                    source=candidate.source,
                    selection_reason=reason,
                    effectiveness_score=effectiveness_score,
                    experiment_cohort=cohort,
                    rule_ref=(
                        candidate.scope_rule_ref
                        if reason == EnumSelectionReason.POLICY_SCOPE_RULE
                        else None
                    ),
                    rank=len(selections) + 1,
                )
            )

        return ModelContextSelectionResult(
            status=EnumSelectionStatus.OK,
            selections=tuple(selections),
            request_id=request.request_id,
            selection_rejected=int(bool(rejected_ids)),
            selection_rejected_ids=rejected_ids,
        )


__all__ = [
    "ContextSelectionPolicyError",
    "HandlerContextSelectionPolicy",
]
