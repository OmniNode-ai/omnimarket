# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide which sprint tickets have enough linked PR, check and runtime evidence."""

from __future__ import annotations

from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumChangeKind,
    EnumCheckStatus,
    EnumCloserVerdict,
    ModelEligibility,
    ModelTicketEvidence,
)


def decide_eligibility(ev: ModelTicketEvidence) -> ModelEligibility:
    missing = list(ev.pr_blockers)
    evidence = [f"linked PR {pr} merged" for pr in ev.linked_prs]
    no_contract = bool(
        ev.contract_error and ev.contract_error.startswith("CONTRACT_MISSING:")
    )
    pr_basis = no_contract and not ev.checks
    known_non_runtime = ev.change_kind is EnumChangeKind.NON_RUNTIME and bool(
        ev.linked_prs
    )
    if not ev.criteria and not (pr_basis and known_non_runtime):
        missing.append(
            "the ticket labels no acceptance criterion (AC<n>), so nothing can be checked"
        )

    if ev.checks:
        bound: dict[str, list[str]] = {}
        for check in ev.checks:
            for label in check.binds:
                bound.setdefault(label, []).append(check.evidence_id)
            if check.status is EnumCheckStatus.PASS:
                where = f" from {ev.contract_ref}" if ev.contract_ref else ""
                binds = f" (binds {', '.join(check.binds)})" if check.binds else ""
                evidence.append(
                    f"dod_verify check {check.evidence_id} passed{binds}{where}"
                )
            elif check.status is EnumCheckStatus.FAIL:
                missing.append(
                    f"dod_verify check {check.evidence_id} failed: {check.detail}".rstrip(
                        ": "
                    )
                )
            else:
                missing.append(
                    f"dod_verify check {check.evidence_id} did not run: {check.detail}".rstrip(
                        ": "
                    )
                )
        for label in ev.criteria:
            if label not in bound:
                missing.append(
                    f"{label} is bound by no dod_verify check in the contract"
                )
        basis = "checks"
    elif pr_basis:
        basis = "linked_prs" if known_non_runtime else "seen_running"
        if not ev.linked_prs:
            missing.append(f"{ev.contract_error}; the ticket links no pull request")
        elif not known_non_runtime:
            unticked = [c for c in ev.criteria if c not in ev.criteria_ticked]
            if unticked:
                missing.append(f"{', '.join(unticked)} not ticked as met on the ticket")
    else:
        basis = ""
        missing.append(ev.contract_error or "dod_verify reported no checks")

    if not known_non_runtime:
        if ev.seen_running:
            evidence.extend(
                f"integration reading SEEN RUNNING: {r}" for r in ev.seen_running
            )
        else:
            missing.append(
                "the integration ledger holds no SEEN RUNNING reading of the change"
            )

    if missing:
        return ModelEligibility(
            ticket_id=ev.ticket_id,
            verdict=EnumCloserVerdict.LEAVE,
            evidence=tuple(evidence),
            missing=tuple(missing),
        )
    return ModelEligibility(
        ticket_id=ev.ticket_id,
        verdict=EnumCloserVerdict.CLOSE,
        basis=basis,
        evidence=tuple(evidence),
    )


class HandlerSprintCloserEligibility:
    """Node handler over :func:`decide_eligibility`."""

    def handle(self, request: ModelTicketEvidence) -> ModelEligibility:
        return decide_eligibility(request)
