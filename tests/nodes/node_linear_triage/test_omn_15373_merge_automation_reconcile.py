# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15373: reverse-path reconciler over merge-automation false-Dones.

Replays the recorded incident. Between 2026-07-28T22:00Z and 2026-07-29T02:30Z
the Linear ``Omninode`` team's ``merge`` git automation minted 16 Done flips
over 15 distinct tickets, every one 2.0-3.1 seconds after its driving PR merged.
Closing keywords were verified absent on six of the driving PRs, so branch-name
linkage alone was sufficient; two more tickets flipped from PRs whose branch does
not contain their identifier, so a bare body mention also suffices. None carried
a ``dod_verify`` receipt AT THE MOMENT OF THE FLIP.

Counting note (the constant here was once named ``_INCIDENT_16``): 16 is the
number of flip ROWS, 15 the number of distinct TICKETS — OMN-14888 appears
twice. A replay is over tickets, so it has 15 members. See ``_INCIDENT_FLIPS``.

The defect these tests lock down
--------------------------------
Under the OMN-14915 decision core as shipped, every one of those flips would
have been **KEPT**. Each has an obvious merged implementing PR, ``evaluate_close_
evidence`` admits ``MERGED_IMPLEMENTING_PR`` as durable evidence, and none of
them bears the cascade fingerprint (they are unrelated tickets across five
repos, not a same-second sibling batch). So the reconciler would have looked at
a wall of false-Dones and approved all of them.

That is the circular-evidence hole: the merge is what CAUSED the flip, so citing
that merge as the flip's evidence restates the trigger instead of proving the
outcome. ``test_red_the_shipped_gate_admits_the_circular_evidence`` pins the
hole open so it cannot silently close; the rest prove the fix.

The opposite error, and why it also has tests here (hazard H1)
--------------------------------------------------------------
Over-reverting is the symmetric failure. Since the incident, OMN-15351 was
hand-reverted and then legitimately re-earned on a ``dod_verify`` receipt, and
is ``Done`` on the live board today. A replay that demanded REVERT_REQUIRED for
every member would pin the demotion of proven work as correct behaviour. The
receipt-backed member is therefore asserted to KEEP, and the reconciler
evaluates the non-circular kinds as a fallback rather than discarding all
evidence the moment a circular kind is present.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_linear_triage.handlers.handler_completion_reconcile import (
    HandlerCompletionReconcile,
)
from omnimarket.nodes.node_linear_triage.models.model_completion_reconcile import (
    MERGE_AUTOMATION_WINDOW_S,
    EnumCompletionVerdict,
    ModelCompletionFacts,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
    evaluate_close_evidence,
)
from omnimarket.nodes.node_linear_triage.services.completion_reconcile import (
    evaluate_completion,
    reconcile_batch,
)

# The recorded incident: (ticket, driving PR, seconds between merge and Done).
# Transcribed from the OMN-15373 blast-radius table.
#
# COUNT, precisely (this constant was previously named ``_INCIDENT_16`` and the
# name encoded a false count): the blast-radius table lists 16 FLIP ROWS over 15
# DISTINCT TICKETS. The 16th row — OMN-14888 via ``onex_change_control#5410``,
# +2.2s — is a SECOND flip of a ticket already present here (via #5409), so
# replaying it adds no distinct verdict. Every claim of "all 16 flips replay"
# should read "all 15 distinct flipped tickets replay".
_INCIDENT_FLIPS: tuple[tuple[str, str, float], ...] = (
    ("OMN-15334", "onex_change_control#5371", 2.7),
    ("OMN-15335", "omninode_infra#740", 2.8),
    ("OMN-15344", "omnimarket#1939", 2.7),
    ("OMN-15347", "omnimarket#1940", 2.2),
    ("OMN-13424", "onex_change_control#5382", 2.0),
    ("OMN-15336", "omnibase_infra#2532", 2.4),
    ("OMN-14974", "omninode_infra#745", 2.7),
    ("OMN-15192", "omnimarket#1941", 2.7),
    ("OMN-15350", "omnimarket#1942", 2.4),
    ("OMN-15190", "omnibase_infra#2534", 2.6),
    ("OMN-15309", "onex_change_control#5401", 2.9),
    ("OMN-14505", "onex_change_control#5401", 2.9),
    ("OMN-15351", "omnimarket#1943", 2.3),
    ("OMN-14888", "onex_change_control#5409", 2.7),
    ("OMN-15365", "omninode_infra#747", 3.1),
)


# Incident tickets for which a COMPLETE probe also resolves NON-circular
# evidence. OMN-15351 is live ``Done`` right now on ``dod_verify run_id
# 002ae20d`` (9/9 verified): its false flip was hand-reverted and the ticket was
# then legitimately re-earned. A replay that asserted REVERT_REQUIRED for it
# would pin the demotion of proven work as correct behaviour — OMN-15373 hazard
# H1. Membership here is a claim about the ticket, not about the reconciler.
_RECEIPT_BACKED: dict[str, ModelCloseEvidence] = {
    "OMN-15351": ModelCloseEvidence(
        kind=EnumCloseEvidenceKind.OCC_RECEIPT,
        detail="dod_verify run_id 002ae20d — 9/9 verified",
    ),
}


def _incident_facts(
    ticket: str,
    pr: str,
    latency: float,
    *,
    evidence: tuple[ModelCloseEvidence, ...] | None = None,
) -> ModelCompletionFacts:
    """One recorded flip, as a COMPLETE live probe would assemble it.

    ``evidence`` defaults to everything a complete probe genuinely resolves for
    the ticket: the merged driving PR (real, but circular for this population)
    PLUS any non-circular receipt the ticket actually holds. Supplying only the
    merged PR would model an INCOMPLETE probe, and baking that into the replay
    is what made the reconciler demote OMN-15351.
    """
    if evidence is None:
        resolved = [
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
                detail=f"{pr} merged",
            )
        ]
        receipt = _RECEIPT_BACKED.get(ticket)
        if receipt is not None:
            resolved.append(receipt)
        evidence = tuple(resolved)
    return ModelCompletionFacts(
        ticket_id=ticket,
        linear_id=f"uuid-{ticket}",
        prior_state_name="In Review",
        started=True,
        # Not a cascade: unrelated tickets across five repos, no sibling batch.
        same_second_sibling_cluster=False,
        parent_completed_same_second=False,
        automation_authored=True,
        merge_to_done_latency_s=latency,
        driving_pr=pr,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# RED: the hole this ticket closes
# ---------------------------------------------------------------------------


def test_red_the_shipped_gate_admits_the_circular_evidence() -> None:
    """The shared evidence vocabulary still ALLOWS a bare merged PR.

    This is the pre-existing behaviour, deliberately unchanged for every other
    caller (OMN-13817's forward auto-close path depends on it). It is asserted
    here so the reason the reconciler needs its own narrowing is visible, and so
    a future global change to ``evaluate_close_evidence`` breaks this test and
    forces a re-read rather than quietly altering the reconciler's meaning.
    """
    decision = evaluate_close_evidence(
        ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            detail="omnimarket#1939 merged",
        )
    )
    assert decision.allowed is True


def test_red_without_the_automation_fingerprint_the_flip_is_kept() -> None:
    """Same facts, fingerprint absent -> KEEP.

    This is exactly what the reconciler did to all 16 before this change: no
    cascade fingerprint + a merged PR = KEEP. The single differentiating input
    is ``automation_authored``/latency, which is what the live probe now
    supplies.
    """
    facts = _incident_facts("OMN-15344", "omnimarket#1939", 2.7).model_copy(
        update={"automation_authored": False, "merge_to_done_latency_s": None}
    )
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.KEEP


# ---------------------------------------------------------------------------
# GREEN: every recorded flip is now REVERT_REQUIRED
# ---------------------------------------------------------------------------


_UNEVIDENCED_FLIPS = tuple(
    row for row in _INCIDENT_FLIPS if row[0] not in _RECEIPT_BACKED
)
_RECEIPT_BACKED_FLIPS = tuple(
    row for row in _INCIDENT_FLIPS if row[0] in _RECEIPT_BACKED
)


@pytest.mark.parametrize(("ticket", "pr", "latency"), _UNEVIDENCED_FLIPS)
def test_each_recorded_flip_is_revert_required(
    ticket: str, pr: str, latency: float
) -> None:
    """Replay: every recorded merge-automation flip with no receipt must REVERT."""
    result = evaluate_completion(_incident_facts(ticket, pr, latency))
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    assert result.automation_fingerprint is True
    assert result.evidence_allowed is False
    assert result.cascade_fingerprint is False
    assert "cannot be its own proof" in result.reason
    assert pr in result.reason


@pytest.mark.parametrize(("ticket", "pr", "latency"), _RECEIPT_BACKED_FLIPS)
def test_receipt_backed_incident_ticket_keeps_its_done(
    ticket: str, pr: str, latency: float
) -> None:
    """OMN-15373 hazard H1, on a LIVE ticket inside this replay corpus.

    OMN-15351 bears the merge-automation fingerprint AND a merged implementing
    PR AND a real ``dod_verify`` receipt, and is ``Done`` on the live board
    today. The receipt is non-circular, so the completion must be KEPT — the
    merged PR being inadmissible must not take the receipt down with it.
    """
    result = evaluate_completion(_incident_facts(ticket, pr, latency))
    assert result.verdict is EnumCompletionVerdict.KEEP
    assert result.evidence_allowed is True
    # The circular kind was still refused; the receipt is what carried it.
    assert result.automation_fingerprint is True
    assert "occ_receipt" in result.reason


def test_whole_incident_batch_splits_by_receipt() -> None:
    """The batch reverts the unevidenced flips and keeps the receipt-backed one."""
    results = reconcile_batch(
        [_incident_facts(t, p, s) for t, p, s in _INCIDENT_FLIPS],
    )
    assert len(results) == len(_INCIDENT_FLIPS)
    by_id = {r.ticket_id: r.verdict for r in results}
    for ticket, _pr, _latency in _UNEVIDENCED_FLIPS:
        assert by_id[ticket] is EnumCompletionVerdict.REVERT_REQUIRED
    for ticket, _pr, _latency in _RECEIPT_BACKED_FLIPS:
        assert by_id[ticket] is EnumCompletionVerdict.KEEP


def test_incomplete_probe_that_drops_the_receipt_still_demotes() -> None:
    """Why the probe's completeness is load-bearing, pinned as a known limit.

    Feed OMN-15351 ONLY the merged PR — the shape an assembler produces when it
    stops at the first resolved fact — and the completion is demoted despite the
    receipt existing in the world. The decision core cannot see facts it was not
    given; this is the contract the live probe (OMN-14915 remainder #1) must
    satisfy: resolve EVERY evidence kind, not the first one.
    """
    facts = _incident_facts(
        "OMN-15351",
        "omnimarket#1943",
        2.3,
        evidence=(
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
                detail="omnimarket#1943 merged",
            ),
        ),
    )
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.REVERT_REQUIRED


def test_receipt_survives_regardless_of_assembler_ordering() -> None:
    """H1 root cause: the verdict must not depend on which fact came first."""
    merged = ModelCloseEvidence(
        kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
        detail="omnimarket#1943 merged",
    )
    receipt = ModelCloseEvidence(
        kind=EnumCloseEvidenceKind.OCC_RECEIPT,
        detail="dod_verify run_id 002ae20d — 9/9 verified",
    )
    for ordering in ((merged, receipt), (receipt, merged)):
        facts = _incident_facts("OMN-15351", "omnimarket#1943", 2.3, evidence=ordering)
        assert evaluate_completion(facts).verdict is EnumCompletionVerdict.KEEP


def test_prior_state_name_is_required_not_defaulted() -> None:
    """H1 aggravator: an omitted prior state must fail loudly, not become Backlog.

    A probe that cannot resolve the pre-Done state used to have ``"Backlog"``
    chosen for it, so reverting an earned Done demoted it below the state it
    actually came from (e.g. ``In Review``).
    """
    with pytest.raises(ValidationError):
        ModelCompletionFacts(ticket_id="OMN-15351")


# ---------------------------------------------------------------------------
# Precision: the reconciler is not a blanket denier
# ---------------------------------------------------------------------------


def test_dod_verify_receipt_keeps_the_completion() -> None:
    """A merge-automation flip backed by a real dod_verify OCC receipt is KEPT.

    This is the whole point of the narrowing: it removes the CIRCULAR evidence,
    not all evidence. Work that was actually proven stays Done.
    """
    facts = _incident_facts(
        "OMN-15255",
        "omnimarket#1944",
        2.5,
        evidence=(
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.OCC_RECEIPT,
                detail="dod_verify run_id ecf77670 — 9/9 verified",
            ),
        ),
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.KEEP
    assert result.evidence_allowed is True


def test_runtime_ops_readback_keeps_the_completion() -> None:
    """The other non-circular kind is also still admissible."""
    facts = _incident_facts(
        "OMN-14168",
        "omnibase_infra#2540",
        2.1,
        evidence=(
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.RUNTIME_OPS_READBACK,
                detail="stability-test /v1/introspection/manifest readback",
            ),
        ),
    )
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.KEEP


@pytest.mark.parametrize(
    "kind",
    [
        EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
        EnumCloseEvidenceKind.SUPERSEDED_BY_MERGED_PR,
        EnumCloseEvidenceKind.ALL_CHILDREN_DONE,
    ],
)
def test_every_circular_kind_is_inadmissible_for_a_merge_flip(
    kind: EnumCloseEvidenceKind,
) -> None:
    """All three merge-derived kinds are stripped.

    ``ALL_CHILDREN_DONE`` is included because a roll-up over children whose own
    Dones were minted by the same automation inherits the circularity one level
    up — it launders the same unproven flips into a parent completion.
    """
    facts = _incident_facts(
        "OMN-15344",
        "omnimarket#1939",
        2.7,
        evidence=(ModelCloseEvidence(kind=kind, detail="omnimarket#1939 merged"),),
    )
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.REVERT_REQUIRED


def test_human_flip_long_after_the_merge_is_not_the_automation_population() -> None:
    """A Done written well outside the automation window is not attributed to it.

    Beyond the window the merge-derived evidence is admissible again, so this
    KEEPs — the narrowing is scoped, not global.
    """
    facts = _incident_facts(
        "OMN-9000", "omnimarket#1900", MERGE_AUTOMATION_WINDOW_S + 1.0
    )
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.KEEP


def test_negative_latency_is_not_the_automation_population() -> None:
    """A Done that predates its 'driving' merge cannot have been caused by it."""
    facts = _incident_facts("OMN-9001", "omnimarket#1901", -5.0)
    result = evaluate_completion(facts)
    assert result.automation_fingerprint is False


def test_unknown_latency_does_not_fabricate_the_fingerprint() -> None:
    """No resolved merge -> no automation attribution.

    Fail-closed is preserved elsewhere: with the fingerprint absent AND no
    evidence, the completion still lands in FLAG_FOR_REVIEW, never KEEP.
    """
    facts = _incident_facts("OMN-9002", "omnimarket#1902", 2.0).model_copy(
        update={"merge_to_done_latency_s": None, "evidence": ()}
    )
    result = evaluate_completion(facts)
    assert result.automation_fingerprint is False
    assert result.verdict is EnumCompletionVerdict.FLAG_FOR_REVIEW


def test_unreadable_probe_still_fails_closed_under_the_automation_path() -> None:
    """An indeterminate evidence probe never upgrades to KEEP here either."""
    facts = _incident_facts(
        "OMN-15344",
        "omnimarket#1939",
        2.7,
        evidence=(
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.OCC_RECEIPT,
                detail="receipt maybe exists, probe could not confirm",
            ),
        ),
    ).model_copy(update={"evidence_probe_ok": False})
    assert evaluate_completion(facts).verdict is EnumCompletionVerdict.REVERT_REQUIRED


# ---------------------------------------------------------------------------
# The revert action: guarded, and it names the right mechanism
# ---------------------------------------------------------------------------


def test_apply_reverts_restores_prior_state_and_explains_the_circularity() -> None:
    """The guard actually reverts the recorded flip and says why."""
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = reconcile_batch([_incident_facts("OMN-15344", "omnimarket#1939", 2.7)])

    report = handler.apply_reverts(client=client, results=results, apply_changes=True)

    client.save_issue.assert_called_once_with(
        issue_id="uuid-OMN-15344", state="In Review"
    )
    body = client.save_comment.call_args.kwargs["body"]
    assert "OMN-15373" in body
    assert "omnimarket#1939" in body
    assert "dod_verify" in body
    assert "not** admissible" in body
    # The cascade explanation must NOT be used — it names the wrong mechanism.
    assert "autoCloseChildIssues" not in body
    assert report.reverted_ticket_ids == ["OMN-15344"]
    assert report.applied is True


def test_dry_run_reverts_nothing_for_the_automation_population() -> None:
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = reconcile_batch([_incident_facts(t, p, s) for t, p, s in _INCIDENT_FLIPS])

    report = handler.apply_reverts(client=client, results=results, apply_changes=False)

    client.save_issue.assert_not_called()
    client.save_comment.assert_not_called()
    assert report.applied is False
    assert report.revert_count == len(_UNEVIDENCED_FLIPS)
    assert report.reverted_ticket_ids == []


def test_cascade_population_still_gets_the_cascade_comment() -> None:
    """No regression: an OMN-14915 cascade revert keeps its own explanation."""
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = reconcile_batch(
        [
            ModelCompletionFacts(
                ticket_id="OMN-14900",
                linear_id="uuid-14900",
                prior_state_name="Backlog",
                same_second_sibling_cluster=True,
                parent_completed_same_second=True,
                evidence=(),
            )
        ]
    )
    handler.apply_reverts(client=client, results=results, apply_changes=True)
    body = client.save_comment.call_args.kwargs["body"]
    assert "autoCloseChildIssues" in body
    assert "OMN-14915" in body
