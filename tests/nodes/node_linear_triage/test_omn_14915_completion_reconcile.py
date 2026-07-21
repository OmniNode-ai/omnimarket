# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14915: fail-closed reverse-path completion reconciler.

Regression coverage for the 2026-07-21T21:07:25Z cascade incident: Linear's
native ``autoCloseChildIssues`` flipped OMN-14889 → 14900/14901/14902/14903 in a
611ms server-side batch with zero acceptance criteria met, bypassing every
in-process forward gate. These tests prove the reconciler:

* KEEPs a genuinely-evidenced completion (not a blanket denier);
* REVERT_REQUIREs an evidence-less completion bearing the cascade fingerprint;
* FLAG_FOR_REVIEWs an evidence-less completion WITHOUT the fingerprint
  (never silently KEEP);
* fails closed when the evidence probe is unreadable (never upgrades to KEEP);
* mutates Linear only under an explicit apply, only for REVERT_REQUIRED.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_completion_reconcile import (
    HandlerCompletionReconcile,
)
from omnimarket.nodes.node_linear_triage.models.model_completion_reconcile import (
    EnumCompletionVerdict,
    ModelCompletionFacts,
    ModelCompletionReconcileStartCommand,
    ModelCompletionVerdictResult,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
)
from omnimarket.nodes.node_linear_triage.services.completion_reconcile import (
    evaluate_completion,
    reconcile_batch,
)

# ---------------------------------------------------------------------------
# Decision core: KEEP when durable evidence is present (not a blanket denier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(EnumCloseEvidenceKind))
def test_keeps_completion_with_each_durable_evidence_kind(
    kind: EnumCloseEvidenceKind,
) -> None:
    """A completion carrying any recognized durable-evidence kind is KEPT —
    even if it also bears the cascade fingerprint, because it is truly done."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-9001",
        started=True,
        same_second_sibling_cluster=True,  # fingerprint present but evidence wins
        evidence=ModelCloseEvidence(kind=kind, detail="PR #900 merged 2026-07-01"),
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.KEEP
    assert result.evidence_allowed is True


# ---------------------------------------------------------------------------
# Decision core: REVERT_REQUIRED for the cascade signature (the incident)
# ---------------------------------------------------------------------------


def test_reverts_evidence_less_same_second_sibling_cluster() -> None:
    """The OMN-14900 case: no durable evidence + same-second sibling batch."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-14900",
        linear_id="689ea29e-946e-4a69-8696-d1498b4c15be",
        prior_state_name="Backlog",
        started=False,
        same_second_sibling_cluster=True,
        parent_completed_same_second=True,
        evidence=None,
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    assert result.evidence_allowed is False
    assert result.cascade_fingerprint is True
    assert "no durable evidence" in result.reason
    assert "Backlog" in result.reason


def test_reverts_evidence_less_parent_completed_same_second() -> None:
    """A grandchild (OMN-14902) closed because its parent completed in the batch."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-14902",
        same_second_sibling_cluster=False,
        parent_completed_same_second=True,
        evidence=None,
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED


def test_empty_detail_evidence_is_not_durable_and_reverts_under_cascade() -> None:
    """A declared kind with a blank detail is refused by the reused gate, so the
    completion is treated as evidence-less (fail-closed)."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-14901",
        same_second_sibling_cluster=True,
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR, detail="   "
        ),
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    assert result.evidence_allowed is False


# ---------------------------------------------------------------------------
# Decision core: FLAG_FOR_REVIEW for evidence-less WITHOUT the fingerprint
# ---------------------------------------------------------------------------


def test_flags_evidence_less_without_cascade_fingerprint() -> None:
    """No durable evidence but no cascade fingerprint -> human review, never KEEP.
    This keeps auto-revert scoped to the cascade population while still failing
    closed (an evidence-less completion is never silently kept)."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-8000",
        started=True,
        same_second_sibling_cluster=False,
        parent_completed_same_second=False,
        evidence=None,
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.FLAG_FOR_REVIEW
    assert result.evidence_allowed is False
    # Fail-closed: it is NOT kept.
    assert result.verdict is not EnumCompletionVerdict.KEEP


# ---------------------------------------------------------------------------
# Decision core: fail-closed on an unreadable/indeterminate evidence probe
# ---------------------------------------------------------------------------


def test_unreadable_probe_is_treated_as_no_evidence() -> None:
    """evidence_probe_ok=False must NEVER upgrade to KEEP, even with an evidence
    object attached — an optional check that silently skips == no check."""
    facts = ModelCompletionFacts(
        ticket_id="OMN-14903",
        same_second_sibling_cluster=True,
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            detail="PR #999 merged (but probe could not confirm)",
        ),
        evidence_probe_ok=False,
    )
    result = evaluate_completion(facts)
    assert result.verdict is EnumCompletionVerdict.REVERT_REQUIRED
    assert result.evidence_allowed is False


# ---------------------------------------------------------------------------
# The incident, end to end: the sweep set reverts, a control KEEPs
# ---------------------------------------------------------------------------


def test_incident_cluster_reverts_and_evidenced_control_keeps() -> None:
    """Model the real 2026-07-21 cluster plus one legitimately-evidenced control.
    The four evidence-less cascade tickets REVERT; the evidenced control KEEPs."""
    incident = [
        ModelCompletionFacts(
            ticket_id=tid,
            same_second_sibling_cluster=True,
            parent_completed_same_second=True,
            evidence=None,
        )
        for tid in ("OMN-14900", "OMN-14901", "OMN-14902", "OMN-14903")
    ]
    control = ModelCompletionFacts(
        ticket_id="OMN-14888",
        started=True,
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            detail="PR #1850 merged 2026-07-21",
        ),
    )
    results = reconcile_batch([*incident, control])
    by_id = {r.ticket_id: r.verdict for r in results}
    assert by_id["OMN-14900"] is EnumCompletionVerdict.REVERT_REQUIRED
    assert by_id["OMN-14901"] is EnumCompletionVerdict.REVERT_REQUIRED
    assert by_id["OMN-14902"] is EnumCompletionVerdict.REVERT_REQUIRED
    assert by_id["OMN-14903"] is EnumCompletionVerdict.REVERT_REQUIRED
    assert by_id["OMN-14888"] is EnumCompletionVerdict.KEEP


# ---------------------------------------------------------------------------
# Handler: dry-run performs ZERO mutations
# ---------------------------------------------------------------------------


def _revert_result(
    ticket_id: str, linear_id: str = "uuid"
) -> ModelCompletionVerdictResult:
    return ModelCompletionVerdictResult(
        ticket_id=ticket_id,
        linear_id=linear_id,
        verdict=EnumCompletionVerdict.REVERT_REQUIRED,
        reason="no durable evidence AND cascade fingerprint",
        evidence_allowed=False,
        prior_state_name="Backlog",
        cascade_fingerprint=True,
    )


def test_apply_reverts_dry_run_makes_no_mutations() -> None:
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = [_revert_result("OMN-14900"), _revert_result("OMN-14901")]
    report = handler.apply_reverts(client=client, results=results, apply_changes=False)
    client.save_issue.assert_not_called()
    client.save_comment.assert_not_called()
    assert report.applied is False
    assert report.revert_count == 2
    assert report.reverted_ticket_ids == []


# ---------------------------------------------------------------------------
# Handler: apply mutates ONLY REVERT_REQUIRED, never KEEP/FLAG
# ---------------------------------------------------------------------------


def test_apply_reverts_mutates_only_revert_required() -> None:
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = [
        _revert_result("OMN-14900", linear_id="id-14900"),
        ModelCompletionVerdictResult(
            ticket_id="OMN-14888",
            linear_id="id-14888",
            verdict=EnumCompletionVerdict.KEEP,
            reason="durable evidence present",
            evidence_allowed=True,
        ),
        ModelCompletionVerdictResult(
            ticket_id="OMN-8000",
            linear_id="id-8000",
            verdict=EnumCompletionVerdict.FLAG_FOR_REVIEW,
            reason="no evidence, no fingerprint",
            evidence_allowed=False,
        ),
    ]
    report = handler.apply_reverts(client=client, results=results, apply_changes=True)

    # Exactly one revert: the REVERT_REQUIRED ticket, to its prior state.
    client.save_issue.assert_called_once_with(issue_id="id-14900", state="Backlog")
    assert client.save_comment.call_count == 1
    # The comment cites the unmet-criteria class.
    comment_body = client.save_comment.call_args.kwargs["body"]
    assert "no durable evidence" in comment_body
    assert "Backlog" in comment_body

    assert report.applied is True
    assert report.reverted_ticket_ids == ["OMN-14900"]
    assert report.keep_count == 1
    assert report.flag_count == 1
    assert report.revert_count == 1


def test_apply_skips_revert_when_linear_id_missing() -> None:
    """Fail-safe: a REVERT_REQUIRED verdict with no linear_id is skipped, not
    mutated with a bad id."""
    handler = HandlerCompletionReconcile()
    client = MagicMock()
    results = [_revert_result("OMN-14900", linear_id="")]
    report = handler.apply_reverts(client=client, results=results, apply_changes=True)
    client.save_issue.assert_not_called()
    assert report.reverted_ticket_ids == []


# ---------------------------------------------------------------------------
# Handler: pure dict dispatch path (RuntimeLocal shim)
# ---------------------------------------------------------------------------


def test_handle_dict_path_returns_report_dict() -> None:
    handler = HandlerCompletionReconcile()
    payload = {
        "completions": [
            {
                "ticket_id": "OMN-14900",
                "same_second_sibling_cluster": True,
                "parent_completed_same_second": True,
            },
            {
                "ticket_id": "OMN-14888",
                "evidence": {
                    "kind": "merged_implementing_pr",
                    "detail": "PR #1850 merged",
                },
            },
        ]
    }
    report = handler.handle(payload)
    assert isinstance(report, dict)
    assert report["revert_count"] == 1
    assert report["keep_count"] == 1
    # handle() is pure — no apply, so nothing reverted.
    assert report["applied"] is False


def test_handle_typed_path_returns_report_model() -> None:
    handler = HandlerCompletionReconcile()
    command = ModelCompletionReconcileStartCommand(
        completions=[
            ModelCompletionFacts(
                ticket_id="OMN-14901",
                same_second_sibling_cluster=True,
                evidence=None,
            )
        ]
    )
    report = handler.handle(command)
    assert not isinstance(report, dict)
    assert report.revert_count == 1
    assert report.results[0].verdict is EnumCompletionVerdict.REVERT_REQUIRED
