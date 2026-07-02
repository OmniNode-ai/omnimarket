# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration proof for the OMN-13854 gate-escape audit handler wiring.

Exercises ``HandlerDodSweepOrchestrator.handle`` end-to-end for
``gate_escape_audit=True`` with the Linear-fetch / gh-search / comment
collaborators injected as deterministic fakes (constructor-injectable seams,
same pattern as the existing ``gh``-backed checks — OMN-13783). No real
network or subprocess call is made.

Non-negotiable regression (design doc §2, "Acceptance regression"): a dry-run
over a fixture reproducing the wf_1628d9a5 batch
(OMN-13797/13798/13799/13800/13802/13803/13805/13788) and the
OMN-13500/13501/13502 batch must REJECT (flag) every one of them, and a
control window containing only legitimate closes (each covered by a distinct
carve-out, or carrying real durable evidence like the real OMN-13817 ticket —
startedAt set + 5 attachments + merged PRs, verified live 2026-07-02) must
produce zero false positives.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_sweep_orchestrator.handlers.handler_dod_sweep_orchestrator import (
    HandlerDodSweepOrchestrator,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.services.gate_escape_audit import (
    EnumGateEscapeCarveOut,
    ModelGateEscapeTicketSnapshot,
)

# The wf_1628d9a5 incident batch — flipped Backlog->Done with zero durable
# evidence (OMN-13817 root-cause description).
_WF_1628D9A5_BATCH = (
    "OMN-13797",
    "OMN-13798",
    "OMN-13799",
    "OMN-13800",
    "OMN-13802",
    "OMN-13803",
    "OMN-13805",
    "OMN-13788",
)

# The earlier batch that showed the same signature (design doc §2 regression list).
_OMN_13500_BATCH = ("OMN-13500", "OMN-13501", "OMN-13502")


def _incident_snapshot(identifier: str) -> ModelGateEscapeTicketSnapshot:
    """A Done ticket carrying the exact wf_1628d9a5 fingerprint."""
    return ModelGateEscapeTicketSnapshot(
        id=f"uuid-{identifier}",
        identifier=identifier,
        title=f"{identifier} incident-batch ticket",
        state_name="Done",
        started_at=None,
        completed_at="2026-07-02T05:19:00Z",
        labels=(),
        attachments_count=0,
        documents_count=0,
        has_children=False,
        all_children_done=False,
    )


def _legit_close_snapshot(
    identifier: str, **overrides: object
) -> ModelGateEscapeTicketSnapshot:
    """A Done ticket with a legitimate closing signal (control fixture)."""
    defaults: dict[str, object] = {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"{identifier} legitimate close",
        "state_name": "Done",
        "started_at": None,
        "completed_at": "2026-07-02T00:00:00Z",
        "labels": (),
        "attachments_count": 0,
        "documents_count": 0,
        "has_children": False,
        "all_children_done": False,
    }
    defaults.update(overrides)
    return ModelGateEscapeTicketSnapshot(**defaults)  # type: ignore[arg-type]


def _run_audit(
    tickets: tuple[ModelGateEscapeTicketSnapshot, ...],
    *,
    merged_pr_ids: frozenset[str] = frozenset(),
    post_comment: bool = False,
    dry_run: bool = True,
):
    posted_comments: list[tuple[str, str]] = []

    handler = HandlerDodSweepOrchestrator(
        linear_fetch_done_tickets_fn=lambda *_args: tickets,
        gh_search_merged_pr_fn=lambda ticket_id: ticket_id in merged_pr_ids,
        linear_post_comment_fn=lambda issue_id, body, _api_key: posted_comments.append(
            (issue_id, body)
        ),
    )
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(
            gate_escape_audit=True,
            linear_api_key="fake-key-for-test",
            post_comment=post_comment,
            dry_run=dry_run,
        )
    )
    return result, posted_comments


# ---------------------------------------------------------------------------
# Non-negotiable regression: known bad batches must be REJECTED
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_wf_1628d9a5_batch_is_flagged() -> None:
    tickets = tuple(_incident_snapshot(tid) for tid in _WF_1628D9A5_BATCH)
    result, _ = _run_audit(tickets)

    assert result.mode == "gate_escape_audit"
    assert result.status == "flagged"
    assert result.gate_escape_checked == len(_WF_1628D9A5_BATCH)
    assert result.gate_escape_flagged == len(_WF_1628D9A5_BATCH)
    flagged_ids = {f.ticket_id for f in result.gate_escape_findings if f.flagged}
    assert flagged_ids == set(_WF_1628D9A5_BATCH)


@pytest.mark.integration
def test_omn_13500_batch_is_flagged() -> None:
    tickets = tuple(_incident_snapshot(tid) for tid in _OMN_13500_BATCH)
    result, _ = _run_audit(tickets)

    assert result.status == "flagged"
    flagged_ids = {f.ticket_id for f in result.gate_escape_findings if f.flagged}
    assert flagged_ids == set(_OMN_13500_BATCH)


# ---------------------------------------------------------------------------
# Non-negotiable regression: control window of legitimate closes -> zero false positives
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_control_window_legitimate_closes_produce_no_false_positives() -> None:
    """One ticket per carve-out class, plus a durably-evidenced close.

    Mirrors the real OMN-13817 ticket (verified live 2026-07-02): Done,
    startedAt set, 5 attachments, 5 merged PRs — the opposite of the incident
    signature despite also being a same-day Done flip.
    """
    control_tickets = (
        _legit_close_snapshot("OMN-90001", state_name="Cancelled"),
        _legit_close_snapshot("OMN-90002", state_name="Duplicate"),
        _legit_close_snapshot("OMN-90003", has_children=True, all_children_done=True),
        _legit_close_snapshot("OMN-90004", labels=("decision-only",)),
        _legit_close_snapshot("OMN-90005", labels=("close-if-done",)),
        # Real OMN-13817 shape: durable evidence via startedAt + attachments.
        _legit_close_snapshot(
            "OMN-13817",
            started_at="2026-07-02T03:20:52.190Z",
            attachments_count=5,
        ),
    )
    result, _ = _run_audit(control_tickets, merged_pr_ids=frozenset({"OMN-90006"}))

    assert result.status == "clean"
    assert result.gate_escape_flagged == 0
    assert result.gate_escape_checked == len(control_tickets)

    carve_outs = {f.ticket_id: f.carve_out for f in result.gate_escape_findings}
    assert carve_outs["OMN-90001"] == EnumGateEscapeCarveOut.CANCEL_STATE
    assert carve_outs["OMN-90002"] == EnumGateEscapeCarveOut.CANCEL_STATE
    assert carve_outs["OMN-90003"] == EnumGateEscapeCarveOut.ALL_CHILDREN_DONE
    assert carve_outs["OMN-90004"] == EnumGateEscapeCarveOut.DECISION_ONLY_LABEL
    assert carve_outs["OMN-90005"] == EnumGateEscapeCarveOut.DECISION_ONLY_LABEL
    # OMN-13817 clears via the durable-evidence path, not a carve-out.
    assert carve_outs["OMN-13817"] is None


@pytest.mark.integration
def test_merged_pr_evidence_clears_a_would_be_flagged_ticket() -> None:
    tickets = (_incident_snapshot("OMN-90006"),)
    result, _ = _run_audit(tickets, merged_pr_ids=frozenset({"OMN-90006"}))

    assert result.status == "clean"
    finding = result.gate_escape_findings[0]
    assert finding.flagged is False
    assert finding.carve_out == EnumGateEscapeCarveOut.MERGED_PR_EVIDENCE


# ---------------------------------------------------------------------------
# post_comment side effect — never mutates state, dry_run suppresses it
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_comment_fires_only_for_flagged_tickets_when_not_dry_run() -> None:
    tickets = (
        _incident_snapshot("OMN-90007"),
        _legit_close_snapshot("OMN-90008", started_at="2026-07-01T00:00:00Z"),
    )
    result, posted = _run_audit(tickets, post_comment=True, dry_run=False)

    assert result.gate_escape_flagged == 1
    assert [issue_id for issue_id, _ in posted] == ["uuid-OMN-90007"]


@pytest.mark.integration
def test_post_comment_suppressed_in_dry_run() -> None:
    tickets = (_incident_snapshot("OMN-90009"),)
    result, posted = _run_audit(tickets, post_comment=True, dry_run=True)

    assert result.gate_escape_flagged == 1
    assert posted == []


@pytest.mark.integration
def test_no_tickets_returns_clean_with_zero_checked() -> None:
    result, posted = _run_audit(())
    assert result.status == "clean"
    assert result.gate_escape_checked == 0
    assert result.gate_escape_flagged == 0
    assert posted == []


@pytest.mark.integration
def test_missing_linear_api_key_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # request.linear_api_key="" falls back to LINEAR_API_KEY — clear it so the
    # test is deterministic regardless of the ambient dev environment.
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    handler = HandlerDodSweepOrchestrator(
        linear_fetch_done_tickets_fn=lambda *_args: (_incident_snapshot("OMN-90010"),),
    )
    result = handler.handle(
        ModelDodSweepOrchestratorRequest(gate_escape_audit=True, linear_api_key="")
    )
    assert result.status == "skipped"
    assert result.details["reason"] == "missing_linear_api_key"
