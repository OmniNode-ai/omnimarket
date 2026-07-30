# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15373: reverse-path reconciler over merge-automation false-Dones.

Replays the recorded incident. Between 2026-07-28T22:00Z and 2026-07-29T02:30Z
the Linear ``Omninode`` team's ``merge`` git automation flipped 16 tickets to
Done, every one 2.0-3.1 seconds after its driving PR merged. Closing keywords
were verified absent on six of the driving PRs, so branch-name linkage alone was
sufficient; two more tickets flipped from PRs whose branch does not contain
their identifier, so a bare body mention also suffices. None of the 16 carried a
``dod_verify`` receipt.

The defect these tests lock down
--------------------------------
Under the OMN-14915 decision core as shipped, every one of those 16 would have
been **KEPT**. Each has an obvious merged implementing PR, ``evaluate_close_
evidence`` admits ``MERGED_IMPLEMENTING_PR`` as durable evidence, and none of
them bears the cascade fingerprint (they are unrelated tickets across five
repos, not a same-second sibling batch). So the reconciler would have looked at
a wall of false-Dones and approved all of them.

That is the circular-evidence hole: the merge is what CAUSED the flip, so citing
that merge as the flip's evidence restates the trigger instead of proving the
outcome. ``test_red_the_shipped_gate_admits_the_circular_evidence`` pins the
hole open so it cannot silently close; the rest prove the fix.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
_INCIDENT_16: tuple[tuple[str, str, float], ...] = (
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


def _incident_facts(
    ticket: str,
    pr: str,
    latency: float,
    *,
    evidence: ModelCloseEvidence | None = None,
) -> ModelCompletionFacts:
    """One recorded flip, as the live probe would assemble it.

    ``evidence`` defaults to the merged driving PR because that is what a probe
    genuinely resolves for these tickets — the merged PR is real. What is false
    is treating it as proof of completion.
    """
    if evidence is None:
        evidence = ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            detail=f"{pr} merged",
        )
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


@pytest.mark.parametrize(("ticket", "pr", "latency"), _INCIDENT_16)
def test_each_recorded_flip_is_revert_required(
    ticket: str, pr: str, latency: float
) -> None:
    """Replay: every one of the recorded merge-automation flips must REVERT."""
    result = evaluate_completion(_incident_facts(ticket, pr, latency))
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    assert result.automation_fingerprint is True
    assert result.evidence_allowed is False
    assert result.cascade_fingerprint is False
    assert "cannot be its own proof" in result.reason
    assert pr in result.reason


def test_whole_incident_batch_reverts() -> None:
    results = reconcile_batch(
        [_incident_facts(t, p, s) for t, p, s in _INCIDENT_16],
    )
    assert len(results) == len(_INCIDENT_16)
    assert all(r.verdict is EnumCompletionVerdict.REVERT_REQUIRED for r in results)


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
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.OCC_RECEIPT,
            detail="dod_verify run_id ecf77670 — 9/9 verified",
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
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.RUNTIME_OPS_READBACK,
            detail="stability-test /v1/introspection/manifest readback",
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
        evidence=ModelCloseEvidence(kind=kind, detail="omnimarket#1939 merged"),
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
        update={"merge_to_done_latency_s": None, "evidence": None}
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
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.OCC_RECEIPT,
            detail="receipt maybe exists, probe could not confirm",
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
    results = reconcile_batch([_incident_facts(t, p, s) for t, p, s in _INCIDENT_16])

    report = handler.apply_reverts(client=client, results=results, apply_changes=False)

    client.save_issue.assert_not_called()
    client.save_comment.assert_not_called()
    assert report.applied is False
    assert report.revert_count == len(_INCIDENT_16)
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
                same_second_sibling_cluster=True,
                parent_completed_same_second=True,
                evidence=None,
            )
        ]
    )
    handler.apply_reverts(client=client, results=results, apply_changes=True)
    body = client.save_comment.call_args.kwargs["body"]
    assert "autoCloseChildIssues" in body
    assert "OMN-14915" in body
