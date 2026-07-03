# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure-logic tests for the OMN-13854 gate-escape audit (L3 close-path layer).

``evaluate_gate_escape`` is the pure evaluation function — no I/O, no gh/Linear
calls — so every carve-out and the bulk-fabrication signature itself are
exercised directly against synthetic ``ModelGateEscapeTicketSnapshot`` fixtures.
The handler-level wiring (injected fetch/gh-search/comment fns, including the
wf_1628d9a5 + OMN-13500 batch regression) lives in
``tests/integration/node_dod_sweep_orchestrator/test_dod_sweep_gate_escape_audit.py``.
"""

from __future__ import annotations

from omnimarket.nodes.node_dod_sweep_orchestrator.services.gate_escape_audit import (
    CANCEL_STATES,
    DECISION_ONLY_LABELS,
    EnumGateEscapeCarveOut,
    ModelGateEscapeTicketSnapshot,
    compute_child_done_rollup,
    evaluate_gate_escape,
)


def _snapshot(**overrides: object) -> ModelGateEscapeTicketSnapshot:
    defaults: dict[str, object] = {
        "id": "uuid-1",
        "identifier": "OMN-99999",
        "title": "Test ticket",
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


# ---------------------------------------------------------------------------
# The wf_1628d9a5 signature itself
# ---------------------------------------------------------------------------


def test_flags_the_bulk_fabrication_signature() -> None:
    """startedAt=null + zero attachments/documents + no merged PR => flagged."""
    finding = evaluate_gate_escape(_snapshot(), merged_pr_found=False)
    assert finding.flagged is True
    assert finding.carve_out is None
    assert "wf_1628d9a5" in finding.reason


def test_does_not_flag_when_started_at_is_set() -> None:
    finding = evaluate_gate_escape(
        _snapshot(started_at="2026-07-02T03:20:52.190Z"), merged_pr_found=False
    )
    assert finding.flagged is False
    assert finding.carve_out is None
    assert "durable evidence" in finding.reason


def test_does_not_flag_when_attachments_present() -> None:
    finding = evaluate_gate_escape(
        _snapshot(attachments_count=5), merged_pr_found=False
    )
    assert finding.flagged is False
    assert finding.carve_out is None


def test_does_not_flag_when_documents_present() -> None:
    finding = evaluate_gate_escape(_snapshot(documents_count=1), merged_pr_found=False)
    assert finding.flagged is False
    assert finding.carve_out is None


def test_empty_string_started_at_treated_as_null() -> None:
    """A blank string is the same absence-of-evidence as None."""
    finding = evaluate_gate_escape(_snapshot(started_at="  "), merged_pr_found=False)
    assert finding.flagged is True


# ---------------------------------------------------------------------------
# Carve-out 1: cancel-class states
# ---------------------------------------------------------------------------


def test_cancel_state_is_carved_out_not_flagged() -> None:
    for state in ("Canceled", "Cancelled", "Duplicate", "Won't Do"):
        finding = evaluate_gate_escape(
            _snapshot(state_name=state), merged_pr_found=False
        )
        assert finding.flagged is False, state
        assert finding.carve_out == EnumGateEscapeCarveOut.CANCEL_STATE, state


def test_cancel_states_constant_matches_known_values() -> None:
    assert (
        frozenset({"canceled", "cancelled", "duplicate", "won't do", "wont do"})
        == CANCEL_STATES
    )


# ---------------------------------------------------------------------------
# Carve-out 2: ALL_CHILDREN_DONE roll-up
# ---------------------------------------------------------------------------


def test_all_children_done_epic_is_carved_out() -> None:
    finding = evaluate_gate_escape(
        _snapshot(has_children=True, all_children_done=True), merged_pr_found=False
    )
    assert finding.flagged is False
    assert finding.carve_out == EnumGateEscapeCarveOut.ALL_CHILDREN_DONE


def test_children_not_all_done_falls_through_to_signature_check() -> None:
    """has_children True but all_children_done False is NOT a carve-out."""
    finding = evaluate_gate_escape(
        _snapshot(has_children=True, all_children_done=False), merged_pr_found=False
    )
    assert finding.flagged is True
    assert finding.carve_out is None


def test_compute_child_done_rollup_all_done() -> None:
    assert compute_child_done_rollup(("Done", "Cancelled", "Done")) is True


def test_compute_child_done_rollup_mixed() -> None:
    assert compute_child_done_rollup(("Done", "In Progress")) is False


def test_compute_child_done_rollup_no_children_is_false() -> None:
    """No children is not a roll-up — the caller gates on has_children separately."""
    assert compute_child_done_rollup(()) is False


# ---------------------------------------------------------------------------
# Carve-out 3: merged PR evidence (implementing OR superseding sibling)
# ---------------------------------------------------------------------------


def test_merged_pr_found_is_carved_out() -> None:
    finding = evaluate_gate_escape(_snapshot(), merged_pr_found=True)
    assert finding.flagged is False
    assert finding.carve_out == EnumGateEscapeCarveOut.MERGED_PR_EVIDENCE
    assert finding.merged_pr_found is True


# ---------------------------------------------------------------------------
# Carve-out 4: decision-only label (explicit label required, not free text)
# ---------------------------------------------------------------------------


def test_decision_only_label_is_carved_out() -> None:
    for label in DECISION_ONLY_LABELS:
        finding = evaluate_gate_escape(
            _snapshot(labels=(label,)), merged_pr_found=False
        )
        assert finding.flagged is False, label
        assert finding.carve_out == EnumGateEscapeCarveOut.DECISION_ONLY_LABEL, label


def test_unrelated_label_does_not_exempt() -> None:
    finding = evaluate_gate_escape(
        _snapshot(labels=("data_pipeline",)), merged_pr_found=False
    )
    assert finding.flagged is True
    assert finding.carve_out is None


def test_label_case_and_whitespace_insensitive() -> None:
    finding = evaluate_gate_escape(
        _snapshot(labels=("  Decision-Only  ",)), merged_pr_found=False
    )
    assert finding.flagged is False
    assert finding.carve_out == EnumGateEscapeCarveOut.DECISION_ONLY_LABEL


# ---------------------------------------------------------------------------
# Carve-out precedence — cancel-state wins even if other signals suggest fabrication
# ---------------------------------------------------------------------------


def test_cancel_state_carve_out_takes_precedence_over_all() -> None:
    finding = evaluate_gate_escape(
        _snapshot(
            state_name="Duplicate",
            has_children=True,
            all_children_done=False,
            labels=(),
        ),
        merged_pr_found=False,
    )
    assert finding.carve_out == EnumGateEscapeCarveOut.CANCEL_STATE
