# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Dropped test_parse_ledger_reads_runs_only_from_declared_cells: requires the ledger parser.
# Dropped test_lab_agent_citation_is_recorded_not_delegating: requires the accounting report.
# Dropped test_lab_agent_zero_is_not_a_citation: requires the accounting report.
# Dropped test_parse_ledger_keeps_counting_legacy_rows_with_inline_cells: requires the ledger parser.
# Dropped test_parse_ledger_declared_cell_beats_inline_free_text: requires the ledger parser.
"""Lab-agent citations and accounting tokens come only from declared cells."""

from __future__ import annotations

import pytest

from omnimarket.handlers import ledger_accounting_cells as da
from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
TS = "2026-10-01T03:00:00Z"
TERMINAL = f"{TS} | TERMINAL | lane=lane-a | friction=none"


@pytest.mark.parametrize(
    "cells",
    [
        "delegated=2 runs=a lab_agent=b,c",
        "delegated=0 delegation_reason=interim-crush lab_agent=aaaaaaaa,bbbbbbbb",
    ],
)
def test_lab_agent_cell_accepted(cells: str) -> None:
    row = f"{TERMINAL} | {cells}"
    assert g.delegation_cell_reason(row) is None
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    "cells",
    [
        "delegated=1 lab_agent=aaaaaaaa-aaaa",
        "delegated=2 | lab_agent=aaaaaaaa,bbbbbbbb",
        "outcome=done delegated=1 lab_agent=x",
        "delegated=1 lab_agent=",
        "delegated=1 lab_agent=none",
        "delegated=1 lab_agent=0",
        "delegated=1 lab_agent=a,,b",
        "lab_agent=x",
        "delegated=0 lab_agent=x",
    ],
)
def test_lab_agent_cell_refused(cells: str) -> None:
    row = f"{TERMINAL} | {cells}"
    reason = g.delegation_cell_reason(row)
    assert reason is not None
    assert reason in g.row_refusals(row)


@pytest.mark.parametrize(
    "prose", ["a stray runs= and runs=none in prose", "friction note runs= "]
)
def test_stray_runs_in_free_text_does_not_blank_the_declared_cell(prose: str) -> None:
    row = f"{TERMINAL} | delegated=1 runs=abc | {prose}"
    assert g.delegation_cell_reason(row) is None


@pytest.mark.parametrize(
    "prose",
    [
        "the lane cited runs=abc in prose",
        "friction=FRICTION 2026-10-01T03:08:16Z runs=abc",
    ],
)
def test_runs_only_in_free_text_is_not_a_declared_cell(prose: str) -> None:
    row = f"{TERMINAL} | delegated=1 | {prose}"
    assert g.delegation_cell_reason(row) is not None


def test_declared_cell_first_value_wins_over_a_later_blank() -> None:
    row = f"{TERMINAL} | delegated=1 runs=abc | runs="
    assert g.delegation_cell_reason(row) is None


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("outcome=done delegated=1 runs=x", [("delegated", "1"), ("runs", "x")]),
        (
            "delegated=0 runs=none (why runs=zzz)",
            [("delegated", "0"), ("runs", "none")],
        ),
        ("Handed the PR; runs=abc", []),
        ("friction=FRICTION 2026-10-01T03:08:16Z runs=abc", []),
        ("lab_agent=a,b", [("lab_agent", "a,b")]),
        ("runs=", [("runs", "")]),
        ("", []),
    ],
)
def test_declared_cell_tokens(cell: str, expected: list[tuple[str, str]]) -> None:
    assert list(da.declared_cell_tokens(cell)) == expected


def test_accounting_cells_trailing_a_free_text_cell_are_not_declared() -> None:
    row = f"{TERMINAL} | Handed the PR over. delegated=1 runs=abc"
    reason = g.delegation_cell_reason(row)
    assert reason is not None
    assert "never from the free text" in reason
