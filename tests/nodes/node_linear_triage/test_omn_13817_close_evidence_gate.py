# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13817: fail-closed durable-evidence gate for Backlog/Todo -> Done flips.

Regression coverage for beta-blocker B7 / W1.4. Close-batch ``wf_1628d9a5``
flipped implementation tickets Backlog->Done with no durable evidence
(``startedAt=null``, zero attachments, zero PRs). These tests prove the gate
refuses a no-evidence close — including in a dry-run of the batch — and that the
node's ``_mark_done`` chokepoint never writes ``Done`` to Linear without evidence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTicket,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    CloseEvidenceRefusedError,
    EnumCloseEvidenceKind,
    ModelCloseEvidence,
    enforce_close_evidence,
    evaluate_close_evidence,
)


def _ticket(identifier: str = "OMN-13797") -> ModelLinearTicket:
    return ModelLinearTicket(
        id=f"id-{identifier}",
        identifier=identifier,
        title="follow-up ticket",
        state="Backlog",
        updated_at="2026-07-01T18:35:39.782Z",
    )


# ---------------------------------------------------------------------------
# Pure gate: ALLOW for each durable kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(EnumCloseEvidenceKind))
def test_gate_allows_each_durable_kind_with_detail(
    kind: EnumCloseEvidenceKind,
) -> None:
    decision = evaluate_close_evidence(
        ModelCloseEvidence(kind=kind, detail="PR #123 merged 2026-07-01")
    )
    assert decision.allowed is True
    assert kind.value in decision.reason


# ---------------------------------------------------------------------------
# Pure gate: REFUSE the no-evidence cases (the wf_1628d9a5 signature)
# ---------------------------------------------------------------------------


def test_gate_refuses_none_evidence() -> None:
    decision = evaluate_close_evidence(None)
    assert decision.allowed is False
    assert "no durable evidence" in decision.reason


def test_gate_refuses_kind_none() -> None:
    decision = evaluate_close_evidence(ModelCloseEvidence(kind=None, detail="x"))
    assert decision.allowed is False
    assert "no durable evidence" in decision.reason


def test_gate_refuses_empty_detail() -> None:
    decision = evaluate_close_evidence(
        ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR, detail="   "
        )
    )
    assert decision.allowed is False
    assert "empty" in decision.reason


def test_enforce_raises_on_no_evidence() -> None:
    with pytest.raises(CloseEvidenceRefusedError) as exc_info:
        enforce_close_evidence(ticket_id="OMN-13797", evidence=None)
    assert exc_info.value.ticket_id == "OMN-13797"
    assert exc_info.value.decision.allowed is False


def test_enforce_returns_allow_decision_with_evidence() -> None:
    decision = enforce_close_evidence(
        ticket_id="OMN-13797",
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.OCC_RECEIPT,
            detail="drift/dod_receipts/OMN-13797/...",
        ),
    )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Handler chokepoint: _mark_done refuses without evidence, never writes Done
# ---------------------------------------------------------------------------


def test_mark_done_refuses_and_never_writes_without_evidence() -> None:
    handler = HandlerLinearTriage()
    client = MagicMock()
    with pytest.raises(CloseEvidenceRefusedError):
        handler._mark_done(
            client=client,
            ticket=_ticket(),
            comment="should never post",
            evidence=ModelCloseEvidence(kind=None, detail=""),
        )
    client.save_issue.assert_not_called()
    client.save_comment.assert_not_called()


def test_mark_done_writes_done_with_durable_evidence() -> None:
    handler = HandlerLinearTriage()
    client = MagicMock()
    ticket = _ticket()
    handler._mark_done(
        client=client,
        ticket=ticket,
        comment="Auto-closed: PR #123 merged",
        evidence=ModelCloseEvidence(
            kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
            detail="PR #123 merged 2026-07-01",
        ),
    )
    client.save_issue.assert_called_once_with(issue_id=ticket.id, state="Done")
    client.save_comment.assert_called_once()


# ---------------------------------------------------------------------------
# Batch: dry-run of the wf_1628d9a5 close-batch — every no-evidence ticket
# is refused, zero Done writes reach Linear.
# ---------------------------------------------------------------------------


def test_dry_run_batch_refuses_every_no_evidence_close() -> None:
    """Simulate the wf_1628d9a5 batch: implementation tickets with no PR / no
    receipt (startedAt=null, zero attachments). The gate must refuse every one
    and the client must receive zero Done writes."""
    handler = HandlerLinearTriage()
    client = MagicMock()

    # The exact tickets the real batch flipped with no durable evidence.
    batch: list[tuple[ModelLinearTicket, ModelCloseEvidence | None]] = [
        (_ticket("OMN-13797"), None),
        (_ticket("OMN-13798"), None),
        (_ticket("OMN-13800"), None),
        (_ticket("OMN-13803"), None),
        (_ticket("OMN-13805"), None),
        (_ticket("OMN-13788"), None),
    ]

    refused: list[str] = []
    closed: list[str] = []
    for ticket, evidence in batch:
        try:
            handler._mark_done(
                client=client,
                ticket=ticket,
                comment="batch close",
                evidence=evidence or ModelCloseEvidence(kind=None, detail=""),
            )
            closed.append(ticket.identifier)
        except CloseEvidenceRefusedError:
            refused.append(ticket.identifier)

    assert closed == []
    assert refused == [t.identifier for t, _ in batch]
    client.save_issue.assert_not_called()
    client.save_comment.assert_not_called()


def test_batch_closes_only_evidenced_tickets() -> None:
    """A mixed batch: only tickets carrying durable evidence get a Done write;
    the no-evidence ones are refused. Proves the gate is a filter, not a blanket
    block."""
    handler = HandlerLinearTriage()

    mixed: list[tuple[ModelLinearTicket, ModelCloseEvidence | None]] = [
        (
            _ticket("OMN-9001"),
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
                detail="PR #900 merged",
            ),
        ),
        (_ticket("OMN-9002"), None),  # no evidence -> refused
        (
            _ticket("OMN-9003"),
            ModelCloseEvidence(
                kind=EnumCloseEvidenceKind.ALL_CHILDREN_DONE,
                detail="All 3 children done",
            ),
        ),
    ]

    closed: list[str] = []
    refused: list[str] = []
    for ticket, evidence in mixed:
        client: Any = MagicMock()
        try:
            handler._mark_done(
                client=client,
                ticket=ticket,
                comment="c",
                evidence=evidence or ModelCloseEvidence(kind=None, detail=""),
            )
            closed.append(ticket.identifier)
            client.save_issue.assert_called_once_with(issue_id=ticket.id, state="Done")
        except CloseEvidenceRefusedError:
            refused.append(ticket.identifier)
            client.save_issue.assert_not_called()

    assert closed == ["OMN-9001", "OMN-9003"]
    assert refused == ["OMN-9002"]
