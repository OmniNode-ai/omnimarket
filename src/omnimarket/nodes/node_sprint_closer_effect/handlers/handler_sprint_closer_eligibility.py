# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one eligibility rule of the sprint closer. Every caller (the one-time triage and the workflow) calls this.

A ticket may close on one of two bases, and on nothing else:

* checks: dod_verify ran the ticket's contract, every acceptance criterion the ticket labels is bound by at least one
  check, every check bound to a criterion passed, and no check of the run failed or was left unrun;
* seen_running: dod_verify found no contract (its checks are absent), the integration ledger holds a reading of the
  change SEEN RUNNING, and every labelled criterion is ticked on the ticket.

A ticket with no labelled criterion, an unbound criterion, a failing or unrun check, or (checks absent) no SEEN
RUNNING reading is left, and ``missing`` names each reason.
"""

from __future__ import annotations

from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumCheckStatus,
    EnumCloserVerdict,
    ModelEligibility,
    ModelTicketEvidence,
)


def decide_eligibility(ev: ModelTicketEvidence) -> ModelEligibility:
    missing: list[str] = []
    evidence: list[str] = []
    if not ev.criteria:
        missing.append("the ticket labels no acceptance criterion (AC<n>), so nothing can be checked")

    if ev.checks:
        bound: dict[str, list[str]] = {}
        for check in ev.checks:
            for label in check.binds:
                bound.setdefault(label, []).append(check.evidence_id)
            if check.status is EnumCheckStatus.PASS:
                where = f" from {ev.contract_ref}" if ev.contract_ref else ""
                binds = f" (binds {', '.join(check.binds)})" if check.binds else ""
                evidence.append(f"dod_verify check {check.evidence_id} passed{binds}{where}")
            elif check.status is EnumCheckStatus.FAIL:
                missing.append(f"dod_verify check {check.evidence_id} failed: {check.detail}".rstrip(": "))
            else:
                missing.append(f"dod_verify check {check.evidence_id} did not run: {check.detail}".rstrip(": "))
        for label in ev.criteria:
            if label not in bound:
                missing.append(f"{label} is bound by no dod_verify check in the contract")
        basis = "checks"
    else:
        basis = "seen_running"
        why = f" ({ev.contract_error})" if ev.contract_error else ""
        if not ev.seen_running:
            missing.append(
                f"no dod_verify check ran{why} and the integration ledger holds no SEEN RUNNING reading of the change"
            )
        else:
            evidence.extend(f"integration reading SEEN RUNNING: {r}" for r in ev.seen_running)
            unticked = [c for c in ev.criteria if c not in ev.criteria_ticked]
            if unticked:
                missing.append(
                    f"no dod_verify check ran{why}; the change reads SEEN RUNNING but "
                    f"{', '.join(unticked)} not ticked as met on the ticket"
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
