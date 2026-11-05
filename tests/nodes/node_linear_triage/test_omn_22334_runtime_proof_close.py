# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22334: the triage node closes a merged ticket only on runtime proof.

A merged implementing PR with no passing runtime receipt at a containing revision leaves
the ticket open as "merged, not yet proven running". This module is about the real
runtime-proof gate, so it opts out of the suite-wide stand-down. The Done-write receipt
gate stays stood down: it is covered by its own module.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from omnimarket.handlers.runtime_proof_close_rules import (
    MERGED_NOT_PROVEN_RUNNING,
    evaluate_proof_close,
)
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumProofTicketKind,
    EnumRuntimeReceiptKind,
    ModelDeclaredProof,
    ModelProofCloseDecision,
    ModelProofCloseRequest,
    ModelRuntimeReceipt,
)
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    CloseEvidenceRefusedError,
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
    enforce_close_evidence,
)
from tests.nodes.node_linear_triage.test_omn_20368_done_write_receipt_gate import (
    _github_with_merged_implementing_pr,
    _issue,
    _linear,
)

USES_REAL_RUNTIME_PROOF_GATE = True

CHANGE = "a" * 40
CONTAINING = "b" * 40
LACKING = "c" * 40


class _Containment:
    def contains(self, *, change_sha: str, revision: str) -> bool | None:
        return revision == CONTAINING


class _Probe:
    """A runtime-proof source: decides from the receipts it was handed."""

    def __init__(
        self,
        receipts: tuple[ModelRuntimeReceipt, ...] = (),
        *,
        ticket_kind: EnumProofTicketKind = EnumProofTicketKind.CODE,
        declared_proof: ModelDeclaredProof | None = None,
    ) -> None:
        self._receipts = receipts
        self._kind = ticket_kind
        self._declared = declared_proof

    def proof_for(
        self, *, ticket_id: str, merged_pr: Mapping[str, str]
    ) -> ModelProofCloseDecision | None:
        if not self._receipts and self._declared is None:
            return None
        return evaluate_proof_close(
            ModelProofCloseRequest(
                ticket_id=ticket_id,
                ticket_kind=self._kind,
                change_merged=True,
                change_sha=CHANGE,
                receipts=self._receipts,
                declared_proof=self._declared,
            ),
            _Containment(),
        )


class _NoVerdict:
    """No dod_verify verdict, so the default runtime-proof source finds no receipt."""

    def verdict_for(self, *, ticket_id: str) -> tuple[None, str]:
        return None, "no verdict"


def _lab_pass(revision: str) -> ModelRuntimeReceipt:
    return ModelRuntimeReceipt(
        kind=EnumRuntimeReceiptKind.LAB_PASS,
        passed=True,
        revision=revision,
        host="h201",
    )


async def _run(handler: HandlerLinearTriage) -> ModelLinearTriageResult:
    return await handler.handle(
        ModelLinearTriageStartCommand(scope="backlog", flag_only=False)
    )


@pytest.mark.unit
class TestMergedOnlyDoesNotClose:
    async def test_merged_only_does_not_close_leaves_ticket_with_reason(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            dod_verdict_probe=_NoVerdict(),
        )
        result = await _run(handler)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        client.save_comment.assert_not_called()
        held = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.HELD_MERGED_NOT_PROVEN_RUNNING
        ]
        assert len(held) == 1
        assert MERGED_NOT_PROVEN_RUNNING in held[0].evidence

    def test_merged_only_does_not_close_at_the_chokepoint(self) -> None:
        for kind in (
            EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            EnumCloseEvidenceKind.SUPERSEDED_BY_MERGED_PR,
        ):
            with pytest.raises(CloseEvidenceRefusedError) as refused:
                enforce_close_evidence(
                    ticket_id="OMN-1",
                    evidence=ModelCloseEvidence(kind=kind, detail="PR #1 merged"),
                )
            assert MERGED_NOT_PROVEN_RUNNING in refused.value.decision.reason

    def test_merged_only_does_not_close_leaves_other_kinds_alone(self) -> None:
        # Narrows only: the OCC receipt and runtime-ops kinds are judged as before.
        for kind in (
            EnumCloseEvidenceKind.OCC_RECEIPT,
            EnumCloseEvidenceKind.RUNTIME_OPS_READBACK,
            EnumCloseEvidenceKind.ALL_CHILDREN_DONE,
        ):
            decision = enforce_close_evidence(
                ticket_id="OMN-1",
                evidence=ModelCloseEvidence(kind=kind, detail="evidence"),
            )
            assert decision.allowed is True


@pytest.mark.unit
class TestRuntimeReceiptDecides:
    async def test_merged_plus_runtime_receipt_closes(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            runtime_proof_probe=_Probe((_lab_pass(CONTAINING),)),
        )
        result = await _run(handler)

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="uuid-1", state="Done")

    async def test_receipt_without_change_does_not_close(self) -> None:
        client = _linear(_issue())
        handler = HandlerLinearTriage(
            client=client,
            github_client=_github_with_merged_implementing_pr(),
            runtime_proof_probe=_Probe((_lab_pass(LACKING),)),
        )
        result = await _run(handler)

        assert result.marked_done == 0
        client.save_issue.assert_not_called()
        held = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.HELD_MERGED_NOT_PROVEN_RUNNING
        ]
        assert len(held) == 1
        assert LACKING in held[0].evidence

    async def test_docs_or_plan_names_declared_proof(self) -> None:
        for kind in (EnumProofTicketKind.DOCS, EnumProofTicketKind.PLAN):
            unmet = _linear(_issue())
            result = await _run(
                HandlerLinearTriage(
                    client=unmet,
                    github_client=_github_with_merged_implementing_pr(),
                    runtime_proof_probe=_Probe(
                        ticket_kind=kind,
                        declared_proof=ModelDeclaredProof(name="plan on the board"),
                    ),
                )
            )
            assert result.marked_done == 0
            unmet.save_issue.assert_not_called()

            met = _linear(_issue())
            result = await _run(
                HandlerLinearTriage(
                    client=met,
                    github_client=_github_with_merged_implementing_pr(),
                    runtime_proof_probe=_Probe(
                        ticket_kind=kind,
                        declared_proof=ModelDeclaredProof(
                            name="plan on the board",
                            satisfied=True,
                            evidence="readback",
                        ),
                    ),
                )
            )
            assert result.marked_done == 1
