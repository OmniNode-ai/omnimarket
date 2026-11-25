# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure rules of the nightly attribution. No clock read, no state, no I/O.

Decision table for a test file F that is red tonight, first row that applies wins:

0. F is absent (or skipped) in the previous night's results: unattributable, ``no_prior_result``.
1. F was red the night before: preexisting.
1a. F passed the night before and no commit landed in between: unattributable, ``no_candidate``.
2. Any candidate with a usable record whose selection skipped F: miss, naming every such suspect.
3. Any candidate without usable evidence: unattributable (a missing record or run is never a pass).
4. Otherwise every candidate selected F and passed: interaction.

The result is a function of the set of candidates: it sorts them, so the order of two pull requests
never changes the class.
"""

from __future__ import annotations

from omnimarket.models.test_selector_reading.model_nightly_attribution import (
    EnumAttributionClass,
    EnumEvidenceState,
    EnumFileOutcome,
    EnumIncompleteReason,
    EnumShardConclusion,
    EnumUnattributableReason,
    ModelAttributionCandidate,
    ModelCandidateEvidence,
    ModelCandidateSelection,
    ModelFileAttribution,
    ModelNightCompleteness,
    ModelNightlyAttribution,
    ModelNightlyAttributionRequest,
)
from omnimarket.models.test_selector_reading.model_test_selector_reading import (
    EnumSelectorReadingState,
)

_RED = frozenset({EnumFileOutcome.FAILED, EnumFileOutcome.ERROR})


def incomplete_reasons(
    night: ModelNightCompleteness,
) -> tuple[EnumIncompleteReason, ...]:
    """Why a night is not complete. Checked whether or not anything failed; empty means complete."""
    reasons: list[EnumIncompleteReason] = []
    if not night.run_terminal:
        reasons.append(EnumIncompleteReason.RUN_NOT_TERMINAL)
    if night.artifacts_found < night.shards_expected:
        reasons.append(EnumIncompleteReason.MISSING_ARTIFACTS)
    if night.shards_in_error > 0:
        reasons.append(EnumIncompleteReason.SHARD_IN_ERROR)
    if night.test_files_reported < night.min_test_files:
        reasons.append(EnumIncompleteReason.TOO_FEW_TEST_FILES)
    return tuple(reasons)


def selects(selection: ModelCandidateSelection, test_file: str) -> bool:
    """True when one of the selection's paths is the file or a directory above it."""
    return any(
        path == test_file or (path.endswith("/") and test_file.startswith(path))
        for path in selection.selected_paths
    )


def _selection(
    candidate: ModelAttributionCandidate, shadow_stage: int | None
) -> ModelCandidateSelection | None:
    record = candidate.record
    if record is None:
        return None
    if shadow_stage is None:
        return record.executed
    return record.by_stage.get(shadow_stage)


def _record_usable(candidate: ModelAttributionCandidate) -> bool:
    record = candidate.record
    if record is None:
        return False
    return candidate.run is None or candidate.run.checkout_sha == record.checkout_sha


def candidate_evidence(
    candidate: ModelAttributionCandidate,
    test_file: str,
    shadow_stage: int | None,
) -> EnumEvidenceState:
    """What the candidate's evidence is worth for one file. Only USABLE counts as a pass."""
    record, run = candidate.record, candidate.run
    if record is None:
        return (
            EnumEvidenceState.DIRECT_PUSH
            if candidate.pr_number is None
            else EnumEvidenceState.MISSING_RECORD
        )
    if run is None:
        return EnumEvidenceState.MISSING_RUN
    if run.checkout_sha != record.checkout_sha:
        return EnumEvidenceState.RECORD_MISMATCH
    if run.cancelled:
        return EnumEvidenceState.CANCELLED_RUN
    if run.completed_at > candidate.merged_at:
        return EnumEvidenceState.FINISHED_AFTER_MERGE
    shards = [
        run.shard_conclusions.get(n) for n in range(1, record.executed.split_count + 1)
    ]
    if any(c is None or c is EnumShardConclusion.SKIPPED for c in shards):
        return EnumEvidenceState.SKIPPED_SHARD
    if any(c is not EnumShardConclusion.SUCCESS for c in shards):
        return EnumEvidenceState.SHARD_NOT_SUCCESS
    if shadow_stage is not None:
        if shadow_stage not in record.by_stage:
            return EnumEvidenceState.MISSING_STAGE_ENTRY
        if not selects(record.executed, test_file):
            return EnumEvidenceState.EXECUTED_SKIPPED_FILE
    return EnumEvidenceState.USABLE


def attribute_file(
    request: ModelNightlyAttributionRequest, test_file: str
) -> ModelFileAttribution:
    shadow = request.shadow_stage is not None
    prior = request.previous_outcomes.get(test_file, EnumFileOutcome.ABSENT)
    if prior in (EnumFileOutcome.ABSENT, EnumFileOutcome.SKIPPED):
        return ModelFileAttribution(
            test_file=test_file,
            klass=EnumAttributionClass.UNATTRIBUTABLE,
            reason=EnumUnattributableReason.NO_PRIOR_RESULT,
            shadow=shadow,
        )
    if prior in _RED:
        return ModelFileAttribution(
            test_file=test_file, klass=EnumAttributionClass.PREEXISTING, shadow=shadow
        )
    candidates = sorted(request.candidates, key=lambda c: c.commit_sha)
    if not candidates:
        return ModelFileAttribution(
            test_file=test_file,
            klass=EnumAttributionClass.UNATTRIBUTABLE,
            reason=EnumUnattributableReason.NO_CANDIDATE,
            shadow=shadow,
        )
    suspects: list[str] = []
    gaps: list[ModelCandidateEvidence] = []
    for candidate in candidates:
        selection = _selection(candidate, request.shadow_stage)
        if (
            selection is not None
            and _record_usable(candidate)
            and not selects(selection, test_file)
        ):
            suspects.append(candidate.commit_sha)
            continue
        state = candidate_evidence(candidate, test_file, request.shadow_stage)
        if state is not EnumEvidenceState.USABLE:
            gaps.append(
                ModelCandidateEvidence(
                    commit_sha=candidate.commit_sha,
                    pr_number=candidate.pr_number,
                    state=state,
                )
            )
    if suspects:
        return ModelFileAttribution(
            test_file=test_file,
            klass=EnumAttributionClass.MISS,
            suspects=tuple(suspects),
            no_evidence=tuple(gaps),
            shadow=shadow,
        )
    if gaps:
        direct = any(g.state is EnumEvidenceState.DIRECT_PUSH for g in gaps)
        return ModelFileAttribution(
            test_file=test_file,
            klass=EnumAttributionClass.UNATTRIBUTABLE,
            reason=(
                EnumUnattributableReason.DIRECT_PUSH
                if direct
                else EnumUnattributableReason.NO_EVIDENCE
            ),
            no_evidence=tuple(gaps),
            shadow=shadow,
        )
    return ModelFileAttribution(
        test_file=test_file, klass=EnumAttributionClass.INTERACTION, shadow=shadow
    )


def attribute_nightly_failures(
    request: ModelNightlyAttributionRequest,
) -> ModelNightlyAttribution:
    """Attribute every red file of the night; an incomplete night attributes nothing."""
    reasons = incomplete_reasons(request.completeness)
    if reasons:
        return ModelNightlyAttribution(
            repo=request.repo,
            slot_date=request.slot_date,
            complete=False,
            incomplete_reasons=reasons,
            reading_state=EnumSelectorReadingState.UNKNOWN,
        )
    red = sorted(f for f, o in request.outcomes.items() if o in _RED)
    attributions = tuple(attribute_file(request, f) for f in red)
    missed = any(a.klass is EnumAttributionClass.MISS for a in attributions)
    return ModelNightlyAttribution(
        repo=request.repo,
        slot_date=request.slot_date,
        complete=True,
        attributions=attributions,
        reading_state=(
            EnumSelectorReadingState.NOT_WORKING
            if missed
            else EnumSelectorReadingState.WORKING
        ),
    )


__all__: list[str] = [
    "attribute_file",
    "attribute_nightly_failures",
    "candidate_evidence",
    "incomplete_reasons",
    "selects",
]
