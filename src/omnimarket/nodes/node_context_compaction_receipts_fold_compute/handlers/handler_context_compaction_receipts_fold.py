# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure, order-independent compaction receipts fold (OMN-17427, TA.13).

Installed and install_mismatch dominate not_installed and uncertain;
installed with install_mismatch is a conflict. A not_installed decided at a
boundary dominates an uncertain one. Prepared copies with differing
assembly_hash are a conflict; differing model output across activations is not.

Copies are deduplicated by model equality. Conflicting identities retain the
smallest session and candidate ids by str; candidate sessions come from prepared
copies when present, otherwise from activated and fallback references.
No I/O or clock is used.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from omnimarket.models.context_compaction import (
    EnumContextInstallUncertainReason,
    ModelContextCompactionActivated,
    ModelContextCompactionInstalled,
    ModelContextCompactionInstallMismatch,
    ModelContextCompactionInstallUncertain,
)
from omnimarket.models.context_compaction_fold import (
    EnumCompactionActivationState,
    EnumCompactionCandidateState,
    ModelCompactionActivationFold,
    ModelCompactionCandidateFold,
    ModelCompactionFallbackFold,
    ModelCompactionFoldRequest,
    ModelCompactionFoldResult,
)

_NOT_INSTALLED_REASONS = frozenset(
    {
        EnumContextInstallUncertainReason.NATIVE_COMPACTION,
        EnumContextInstallUncertainReason.NOT_SELECTED,
    }
)


def _group[T](
    receipts: tuple[T, ...], identity: Callable[[T], UUID | None]
) -> dict[UUID, set[T]]:
    groups: dict[UUID, set[T]] = {}
    for receipt in receipts:
        key = identity(receipt)
        if key is not None:
            groups.setdefault(key, set()).add(receipt)
    return groups


def _fold_activation(
    activation_id: UUID,
    activated: set[ModelContextCompactionActivated],
    installed: set[ModelContextCompactionInstalled],
    mismatch: set[ModelContextCompactionInstallMismatch],
    uncertain: set[ModelContextCompactionInstallUncertain],
) -> ModelCompactionActivationFold:
    sessions = (
        {e.session_id for e in activated}
        | {e.session_id for e in installed}
        | {e.session_id for e in mismatch}
        | {e.session_id for e in uncertain}
    )
    not_installed = {e for e in uncertain if e.reason in _NOT_INSTALLED_REASONS}
    conflicts = tuple(
        sorted(
            label
            for label, present in (
                ("activated_copies_differ", len(activated) > 1),
                ("installed_and_mismatch", bool(installed and mismatch)),
                ("installed_copies_differ", len(installed) > 1),
                ("mismatch_copies_differ", len(mismatch) > 1),
                ("not_installed_reasons_differ", len(not_installed) > 1),
                ("session_differs", len(sessions) > 1),
            )
            if present
        )
    )
    state = EnumCompactionActivationState.ACTIVATED
    reason = None
    boundary_uuid = None
    observed_hash = None
    unreadable = [
        e
        for e in uncertain
        if e.reason is EnumContextInstallUncertainReason.UNPARSEABLE_RECORD
    ]
    if conflicts:
        state = EnumCompactionActivationState.CONFLICT
    elif installed:
        state = EnumCompactionActivationState.INSTALLED
        fact = next(iter(installed))
        boundary_uuid, observed_hash = fact.boundary_uuid, fact.observed_hash
    elif mismatch:
        state = EnumCompactionActivationState.INSTALL_MISMATCH
        mismatch_fact = next(iter(mismatch))
        boundary_uuid, observed_hash = (
            mismatch_fact.boundary_uuid,
            mismatch_fact.observed_hash,
        )
    elif not_installed:
        state = EnumCompactionActivationState.NOT_INSTALLED
        decision = next(iter(not_installed))
        reason, boundary_uuid = decision.reason, decision.boundary_uuid
    elif unreadable:
        state = EnumCompactionActivationState.UNCERTAIN
        decision = min(
            unreadable,
            key=lambda e: (e.boundary_uuid is None, str(e.boundary_uuid)),
        )
        reason, boundary_uuid = decision.reason, decision.boundary_uuid
    elif uncertain:
        state = EnumCompactionActivationState.UNCERTAIN
        reason = EnumContextInstallUncertainReason.CAPTURE_GAP
    return ModelCompactionActivationFold(
        activation_id=activation_id,
        session_id=min(sessions, key=str),
        candidate_id=min((e.candidate_id for e in activated), key=str, default=None),
        activated=bool(activated),
        state=state,
        reason=reason,
        boundary_uuid=boundary_uuid,
        observed_hash=observed_hash,
        conflicts=conflicts,
    )


class HandlerContextCompactionReceiptsFold:
    """Fold a set of receipts into deterministic candidate, activation and fallback rows."""

    def handle(self, request: ModelCompactionFoldRequest) -> ModelCompactionFoldResult:
        prepared = _group(request.prepared, lambda e: e.candidate_id)
        candidate_activations = _group(request.activated, lambda e: e.candidate_id)
        candidate_fallbacks = _group(request.fallback, lambda e: e.candidate_id)
        candidates: list[ModelCompactionCandidateFold] = []
        for candidate_id in sorted(
            prepared.keys() | candidate_activations.keys() | candidate_fallbacks.keys(),
            key=str,
        ):
            copies = prepared.get(candidate_id, set())
            sessions = {e.session_id for e in copies} or (
                {e.session_id for e in candidate_activations.get(candidate_id, set())}
                | {e.session_id for e in candidate_fallbacks.get(candidate_id, set())}
            )
            state = EnumCompactionCandidateState.UNPREPARED
            if len(copies) == 1:
                state = EnumCompactionCandidateState.PREPARED
            elif len(copies) > 1:
                state = EnumCompactionCandidateState.CONFLICT
            candidates.append(
                ModelCompactionCandidateFold(
                    candidate_id=candidate_id,
                    session_id=min(sessions, key=str, default=None),
                    state=state,
                    assembly_hashes=tuple(sorted({e.assembly_hash for e in copies})),
                    activation_ids=tuple(
                        sorted(
                            {
                                e.activation_id
                                for e in candidate_activations.get(candidate_id, set())
                            },
                            key=str,
                        )
                    ),
                    fallback_invocation_ids=tuple(
                        sorted(
                            {
                                e.invocation_id
                                for e in candidate_fallbacks.get(candidate_id, set())
                            },
                            key=str,
                        )
                    ),
                )
            )

        activated = _group(request.activated, lambda e: e.activation_id)
        installed = _group(request.installed, lambda e: e.activation_id)
        mismatch = _group(request.install_mismatch, lambda e: e.activation_id)
        uncertain = _group(request.install_uncertain, lambda e: e.activation_id)
        activation_ids = (
            activated.keys() | installed.keys() | mismatch.keys() | uncertain.keys()
        )
        fallbacks = _group(request.fallback, lambda e: e.invocation_id)
        return ModelCompactionFoldResult(
            candidates=tuple(candidates),
            activations=tuple(
                _fold_activation(
                    activation_id,
                    activated.get(activation_id, set()),
                    installed.get(activation_id, set()),
                    mismatch.get(activation_id, set()),
                    uncertain.get(activation_id, set()),
                )
                for activation_id in sorted(activation_ids, key=str)
            ),
            fallbacks=tuple(
                ModelCompactionFallbackFold(
                    invocation_id=invocation_id,
                    session_id=min((e.session_id for e in copies), key=str),
                    candidate_id=min(
                        (e.candidate_id for e in copies if e.candidate_id is not None),
                        key=str,
                        default=None,
                    ),
                    failure_classes=tuple(sorted({e.failure_class for e in copies})),
                    conflict=len(copies) > 1,
                )
                for invocation_id, copies in sorted(
                    fallbacks.items(), key=lambda item: str(item[0])
                )
            ),
        )
