# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Red-first tests for the dry-run board-truth reconciler.

Two things are proven here: the ledger parser handles the real file's messy row
grammar, and the reconciler has no write path at all — dry-run is structural,
not a flag someone can invert.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16732: this node
    - OMN-16733: the live writer, which does not exist yet by design
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelLinearStateFact,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.handlers.handler_board_truth_reconcile import (
    HandlerBoardTruthReconcile,
    parse_ledger_claims,
)
from omnimarket.nodes.node_board_truth_reconcile_effect.models.model_board_reconcile_input import (
    ModelBoardReconcileInput,
)

_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_CORRELATION = UUID("00000000-0000-4000-8000-000000000002")

# Verbatim row shapes taken from docs/tracking/ROLLING_WORK_LEDGER.md. The
# grammar is genuinely heterogeneous — leading pipe optional, ticket field
# sometimes before the verb and sometimes only in the body, separators mixed.
_REAL_ROWS = """
| 2026-08-27 | OMN-16729, OMN-16730 | CLAIM | lane board-truth-mechanization. Implementing now. |
2026-08-27T11:08:16Z | omn16558-verify | OMN-16558,OMN-16594 | TERMINAL | READ-ONLY verification complete. |
| 2026-08-27T11:09:56Z | onexdev-ownership-optionA | TERMINAL | OMN-16685 + OMN-16702 Option A BUILT + MERGED. |
2026-08-26T19:55:00Z | queue-tail-closeouts-2 | OMN-16589/OMN-16581 | CLAIM | Successor watch lane. |
2026-08-27T11:12:00Z | idle-sched-archaeology | CLAIM | READ-ONLY archaeology sweep, no ticket cited. |
Some prose line that mentions OMN-99999 but is not a ledger row at all.
"""


def _state(name: str, state_type: str = "started") -> ModelLinearStateFact:
    return ModelLinearStateFact(
        state_name=name,
        state_type=state_type,
        set_at=_NOW - timedelta(days=1),
        set_by_automation=None,
    )


def _bundle(
    ticket: str, state_name: str, state_type: str = "started"
) -> ModelBoardFactBundle:
    return ModelBoardFactBundle(
        ticket=ticket,
        current_state=_state(state_name, state_type),
        ledger_claims=(),
        pull_requests=(),
        branches=(),
        facts_complete=True,
    )


def _run(tmp_path: Path | None = None, *bundles: ModelBoardFactBundle):
    handler = HandlerBoardTruthReconcile()
    return handler.handle(
        ModelBoardReconcileInput(
            correlation_id=_CORRELATION,
            evaluated_at=_NOW,
            staleness_days=3,
            ledger_path=tmp_path,
            tickets=bundles,
        )
    )


# --- ledger parsing --------------------------------------------------------


@pytest.mark.unit
def test_parses_claim_rows_with_a_leading_pipe() -> None:
    claims = parse_ledger_claims(_REAL_ROWS)
    assert "OMN-16729" in claims
    assert claims["OMN-16729"][0].terminal_at is None


@pytest.mark.unit
def test_parses_rows_without_a_leading_pipe() -> None:
    claims = parse_ledger_claims(_REAL_ROWS)
    assert "OMN-16558" in claims
    assert claims["OMN-16558"][0].terminal_at is not None


@pytest.mark.unit
def test_handles_slash_separated_ticket_lists() -> None:
    claims = parse_ledger_claims(_REAL_ROWS)
    assert "OMN-16589" in claims
    assert "OMN-16581" in claims


@pytest.mark.unit
def test_falls_back_to_the_body_when_no_ticket_precedes_the_verb() -> None:
    claims = parse_ledger_claims(_REAL_ROWS)
    assert "OMN-16685" in claims, "row 19318 cites its tickets only in the body"


@pytest.mark.unit
def test_ignores_prose_lines_that_are_not_ledger_rows() -> None:
    claims = parse_ledger_claims(_REAL_ROWS)
    assert "OMN-99999" not in claims


@pytest.mark.unit
def test_a_row_with_no_ticket_anywhere_is_dropped_not_guessed() -> None:
    claims = parse_ledger_claims(
        "2026-08-27T11:12:00Z | idle-sched-archaeology | CLAIM | no ticket cited |"
    )
    assert claims == {}


@pytest.mark.unit
def test_parser_is_deterministic() -> None:
    assert parse_ledger_claims(_REAL_ROWS) == parse_ledger_claims(_REAL_ROWS)


# --- reconciler ------------------------------------------------------------


@pytest.mark.unit
def test_merges_parsed_claims_into_the_fact_bundles(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "| 2026-08-27T10:00:00Z | some-lane | OMN-11111 | CLAIM | live work |\n",
        encoding="utf-8",
    )
    output = _run(ledger, _bundle("OMN-11111", "Backlog", "backlog"))
    row = output.projection.rows[0]
    assert row.action is EnumBoardReconcileAction.FLIP
    assert any("live ledger CLAIM" in e for e in row.evidence)


@pytest.mark.unit
def test_report_lists_flip_rows_with_their_evidence(tmp_path: Path) -> None:
    output = _run(None, _bundle("OMN-15579", "In Progress"))
    assert "OMN-15579" in output.report
    assert "FLIP" in output.report
    assert "0 open PRs" in output.report


@pytest.mark.unit
def test_report_separates_discrepancies_from_flips() -> None:
    output = _run(
        None,
        _bundle("OMN-A", "In Progress"),
        ModelBoardFactBundle(
            ticket="OMN-B",
            current_state=_state("In Review"),
            ledger_claims=(),
            pull_requests=(),
            branches=(),
            facts_complete=False,
        ),
    )
    assert "DISCREPANCIES" in output.report
    assert output.summary.flip_count == 1
    assert output.summary.discrepancy_count == 1


@pytest.mark.unit
def test_summary_counts_rows_needing_human_confirmation() -> None:
    output = _run(None, _bundle("OMN-A", "In Progress"))
    assert output.summary.requires_confirmation_count == 1


@pytest.mark.unit
def test_rerunning_over_unchanged_facts_yields_an_identical_report() -> None:
    bundles = (_bundle("OMN-A", "In Progress"), _bundle("OMN-B", "Backlog", "backlog"))
    first = _run(None, *bundles)
    second = _run(None, *bundles)
    assert first.report == second.report
    assert first.summary == second.summary


@pytest.mark.unit
def test_reconciler_exposes_no_write_path() -> None:
    """Dry-run is structural. There is nothing here that could mutate Linear."""
    handler = HandlerBoardTruthReconcile()
    forbidden = {"write", "flip", "apply", "mutate", "update_issue", "arm"}
    exposed = {name for name in dir(handler) if not name.startswith("_")}
    assert not (exposed & forbidden), (
        f"unexpected write-shaped surface: {exposed & forbidden}"
    )


@pytest.mark.unit
def test_input_model_rejects_an_arm_flag() -> None:
    """A caller must not be able to smuggle in a live-write flag."""
    with pytest.raises(ValidationError, match="dry_run"):
        ModelBoardReconcileInput(
            correlation_id=_CORRELATION,
            evaluated_at=_NOW,
            staleness_days=3,
            ledger_path=None,
            tickets=(),
            dry_run=False,
        )


@pytest.mark.unit
def test_terminal_closes_its_own_lane_claim_rather_than_adding_a_row() -> None:
    """Without pairing, every abandoned lane would leave a live claim behind."""
    claims = parse_ledger_claims(
        "2026-08-24T09:00:00Z | lane-a | OMN-90002 | CLAIM | building |\n"
        "2026-08-24T18:00:00Z | lane-a | OMN-90002 | TERMINAL | abandoned |\n"
    )
    assert len(claims["OMN-90002"]) == 1
    assert claims["OMN-90002"][0].terminal_at is not None


@pytest.mark.unit
def test_a_terminal_does_not_close_another_lanes_claim() -> None:
    claims = parse_ledger_claims(
        "2026-08-24T09:00:00Z | lane-a | OMN-90002 | CLAIM | still building |\n"
        "2026-08-24T18:00:00Z | lane-b | OMN-90002 | TERMINAL | different lane |\n"
    )
    live = [c for c in claims["OMN-90002"] if c.terminal_at is None]
    assert len(live) == 1
    assert live[0].lane == "lane-a"


@pytest.mark.unit
def test_an_orphan_terminal_is_recorded_as_already_closed() -> None:
    """A lane whose CLAIM predates the retained window still reads as closed."""
    claims = parse_ledger_claims(
        "2026-08-24T18:00:00Z | lane-a | OMN-90002 | TERMINAL | no prior claim row |\n"
    )
    assert claims["OMN-90002"][0].terminal_at is not None
