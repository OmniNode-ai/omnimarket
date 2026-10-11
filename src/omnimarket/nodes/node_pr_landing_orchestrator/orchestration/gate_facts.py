# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The arm decision's ledger facts: HOLD rows in force and the head's lab pass (OMN-20866).

Pure: :func:`decide_gate` turns what the projections said
(:class:`ModelPrLandingGateFacts`) into ARM-eligible or one named reason, in
this order, so the first stop is the one named:

1. the ledger projection unreadable, or not moved within its freshness bound:
   ``ledger_holds_unknown`` (a hold written since cannot be seen);
2. a HOLD row in force for the PR: ``ledger_hold_in_force`` with its ids;
3. in a lab-proof repository (rule 24), the lab proof projection unreadable:
   ``lab_pass_unknown``;
4. in a lab-proof repository, no PASS for the exact head: ``lab_pass_missing``.

A HOLD is in force by the landing controller's canonical hold check
(drain_map ``hold_findings``) over the projection's open HOLD entities, whose
open state already applies ``RELEASE re=<id>``: a lease on a proof surface
(``surface=``) and the fixer kill switch (``scope=fixer``) hold no PR; an
``until=`` in the past has expired; otherwise the hold names the PR in its
``pr=`` cell, or, with no ``pr=`` cell, names the PR's repository in ``repo=``
or the PR itself in its first line.

A lab pass counts only for the exact head: a pr-head receipt with result PASS
that its verifier also passed (token PASS), or a lab pool ``LAB PROOF PASS``
readback whose recorded head (ten or more characters) starts the head.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_gate_facts import (
    EnumPrLandingFactState,
    EnumPrLandingWithheldReason,
    ModelPrLandingGateDecision,
    ModelPrLandingGateFacts,
    ModelPrLandingLabPass,
    ModelPrLandingLedgerHold,
)

_OWNER = "OmniNode-ai"
_PASS = "PASS"
_FIELD = r"(?:^|[\s|])"
_SURFACE_RE = re.compile(_FIELD + r"surface=")
_FIXER_SCOPE_RE = re.compile(r"(?:^|[\s|])scope=fixer(?=[\s|]|$)")
_SHORT_HEAD = 12


def _short_repo(repository: str) -> str:
    return repository.split("/", 1)[1] if "/" in repository else repository


def _names_pr(text: str, repo: str, pr_number: int) -> bool:
    """``<repo>#<n>`` (optionally ``OmniNode-ai/``-prefixed) as a whole token."""
    pattern = rf"(?<![\w-])(?:{_OWNER}/)?{re.escape(repo)}#{pr_number}(?!\d)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _names_repo(cell: str, repo: str) -> bool:
    return any(
        _short_repo(token.strip()).lower() == repo.lower()
        for token in re.split(r"[,\s]+", cell)
        if token.strip()
    )


def holds_in_force(
    holds: Iterable[ModelPrLandingLedgerHold],
    *,
    repository: str,
    pr_number: int,
    now: datetime,
) -> tuple[str, ...]:
    """The ids of the open HOLD entities that hold this PR now, in the order given."""
    repo = _short_repo(repository)
    held: list[str] = []
    for hold in holds:
        first = hold.raw_row.split("\n", 1)[0]
        if hold.surface or _SURFACE_RE.search(first):
            continue  # a lease on a proof surface holds the surface, not a PR
        if _FIXER_SCOPE_RE.search(first):
            continue  # the fixer kill switch stops workers, never a merge
        if hold.until_at is not None and hold.until_at <= now:
            continue
        if hold.pr:
            if _names_pr(hold.pr, repo, pr_number):
                held.append(hold.hold_id)
            continue
        if (hold.repo and _names_repo(hold.repo, repo)) or _names_pr(
            first, repo, pr_number
        ):
            held.append(hold.hold_id)
    return tuple(held)


def _passes_head(lab_pass: ModelPrLandingLabPass, head_sha: str) -> bool:
    if lab_pass.result != _PASS:
        return False
    if lab_pass.source == "lab_proof_receipt":
        return lab_pass.verifier_token == _PASS and lab_pass.head_sha == head_sha
    return head_sha.startswith(lab_pass.head_sha)


def decide_gate(
    facts: ModelPrLandingGateFacts,
    *,
    now: datetime,
    lab_proof_required: bool,
    ledger_freshness_bound: timedelta,
) -> ModelPrLandingGateDecision:
    """ARM-eligible, or the first fact that stops the arm, named."""
    unknown = facts.unknown_detail or "unreadable"
    if facts.holds_state is EnumPrLandingFactState.UNKNOWN:
        return ModelPrLandingGateDecision(
            withheld=EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN, detail=unknown
        )
    newest = facts.ledger_newest_projected_at
    if newest is None or now - newest > ledger_freshness_bound:
        seen = newest.isoformat() if newest is not None else "never"
        return ModelPrLandingGateDecision(
            withheld=EnumPrLandingWithheldReason.LEDGER_HOLDS_UNKNOWN,
            detail=(
                f"the ledger projection's newest row was projected {seen}, "
                f"older than its {int(ledger_freshness_bound.total_seconds() // 60)}"
                " minute bound"
            ),
        )
    hold_ids = holds_in_force(
        facts.holds,
        repository=facts.repository,
        pr_number=facts.pr_number,
        now=now,
    )
    if hold_ids:
        return ModelPrLandingGateDecision(
            withheld=EnumPrLandingWithheldReason.LEDGER_HOLD_IN_FORCE,
            detail=", ".join(hold_ids),
            hold_ids=hold_ids,
        )
    if not lab_proof_required:
        return ModelPrLandingGateDecision()
    if facts.lab_state is EnumPrLandingFactState.UNKNOWN:
        return ModelPrLandingGateDecision(
            withheld=EnumPrLandingWithheldReason.LAB_PASS_UNKNOWN, detail=unknown
        )
    proof = next((p for p in facts.lab_passes if _passes_head(p, facts.head_sha)), None)
    if proof is None:
        return ModelPrLandingGateDecision(
            withheld=EnumPrLandingWithheldReason.LAB_PASS_MISSING,
            detail=f"no PASS lab proof for head {facts.head_sha[:_SHORT_HEAD]}",
        )
    return ModelPrLandingGateDecision(lab_pass_ref=proof.ref)


__all__: list[str] = ["decide_gate", "holds_in_force"]
