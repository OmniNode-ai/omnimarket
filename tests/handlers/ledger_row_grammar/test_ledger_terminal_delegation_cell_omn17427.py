# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Rewrote test_cli_refuses_missing_cell_then_lands_accounted_terminal; dropped retired append command I/O.
# Rewrote both test_row_verb_requires_accounting_or_machine_identity cases; dropped CLI verb writes.
# Rewrote test_cli_exits_65_on_lane_authored_empty_why; dropped retired append command I/O.
"""TERMINAL delegation accounting, machine exemptions and lane-authored evidence."""

from __future__ import annotations

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
TS = "2026-10-01T03:00:00Z"
TERMINAL = f"{TS} | TERMINAL | lane=lane-a | friction=none"


@pytest.mark.parametrize(
    "cells",
    [
        "delegated=2 runs=a,b",
        "delegated=1 runs=x codex=y",
        "delegated=1 runs=x codex=y jev=z",
        "delegated=0 delegation_reason=no text step",
        "delegated=2 | runs=a,b",
        "delegated=0 | delegation_reason=no text step",
        "outcome=done delegated=1 runs=x",
        "outcome=done delegated=0 delegation_reason=no text step",
        # Live shapes measured on 2026-09-28 (onex-delegation-accounting reads them too):
        "delegated=1 codex=aaaaaaaa-bbbb",
        "delegated=2 jev=run-a,run-b",
        "delegated=0 (no text-shaped step)",
        "delegated=0 runs=none (mechanical single-line YAML edit)",
        "delegated=0, delegation_reason=pure live-state reads",
        "delegation=na:conflict-only",
        "lab=2 runs=run-a",
        "delegated=1 runs=run-a lab=1 glm=0",
    ],
)
def test_accepted_shapes(cells: str) -> None:
    row = f"{TERMINAL} | {cells}"
    assert g.delegation_cell_reason(row) is None
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    "cells",
    [
        "outcome=done",
        "delegated=0",
        "delegated=0 delegation_reason=",
        "delegated=0 | delegation_reason=   | outcome=done",
        "delegated=0 delegation_reason= codex=y",
        "delegated=3",
        "delegated=3 runs=",
        "delegated=3 runs=a,,b",
        "delegated=abc runs=x",
        "delegated=-1 runs=x",
        "delegated=0 ()",
        "delegated=1 codex=none",
        "not-delegated=1 runs=x",
        "delegated=0 not-delegation_reason=why",
    ],
)
def test_refused_shapes(cells: str) -> None:
    row = f"{TERMINAL} | {cells}"
    reason = g.delegation_cell_reason(row)
    assert reason is not None
    assert "OMN-17427" in reason
    assert "`delegated=<n> runs=<ids>`" in reason
    assert "`delegated=0 delegation_reason=<why>`" in reason
    assert reason in g.row_refusals(row)


@pytest.mark.parametrize(
    "reason", ["", "UPPERCASE", "has space", "-", "none", "n/a", "x"]
)
def test_typed_na_refuses_malformed_reasons(reason: str) -> None:
    refusal = g.delegation_cell_reason(f"{TERMINAL} | delegation=na:{reason}")
    assert refusal is not None
    assert "^[a-z0-9][a-z0-9-]{1,40}$" in refusal


def test_typed_na_must_start_a_token() -> None:
    refusal = g.delegation_cell_reason(f"{TERMINAL} | xdelegation=na:foo")
    assert refusal is not None
    assert "`delegation=na:<reason>`" in refusal


def test_lab_and_glm_counts_need_runs_ids() -> None:
    refusal = g.delegation_cell_reason(f"{TERMINAL} | lab=1")
    assert refusal == "lab=/glm= counts need the runs= ids that back them"


def test_non_integer_glm_count_is_refused() -> None:
    refusal = g.delegation_cell_reason(f"{TERMINAL} | glm=abc")
    assert refusal is not None
    assert "not an integer count" in refusal


@pytest.mark.parametrize(
    "identity",
    ["actor=script:arm_green_prs", "actor=launchd:pr-watcher", "model=none"],
)
@pytest.mark.parametrize("prefix", ["", "outcome=done "])
def test_machine_writers_are_exempt(identity: str, prefix: str) -> None:
    assert g.row_refusals(f"{TERMINAL} | {prefix}{identity}") == []


@pytest.mark.parametrize("identity", ["actor=agent:script:x", "model=none-extra"])
def test_other_identities_are_not_exempt(identity: str) -> None:
    assert g.delegation_cell_reason(f"{TERMINAL} | {identity}") is not None


@pytest.mark.parametrize("kind", ["CLAIM", "STATUS", "FRICTION"])
def test_other_row_types_are_untouched(kind: str) -> None:
    row = f"{TS} | {kind} | lane=lane-a | ticket=OMN-17427 | worktree=none | delegated=abc"
    assert g.delegation_cell_reason(row) is None
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    "alias", [key for key, value in g.LEGACY_TYPE_MAP.items() if value == "TERMINAL"]
)
def test_terminal_aliases_keep_type_refusal_and_require_accounting(alias: str) -> None:
    row = TERMINAL.replace("| TERMINAL |", f"| {alias} |")
    assert any("legacy type" in reason for reason in g.row_refusals(row))
    assert any("OMN-17427" in reason for reason in g.row_refusals(row))
    accounted = row + " | delegated=1 runs=x"
    assert g.delegation_cell_reason(accounted) is None
    assert len(g.row_refusals(accounted)) == 1


AUTHORED = "delegation_reason=lane-authored:no-route"


def test_lane_authored_with_why_and_pr_is_accepted() -> None:
    row = f"{TERMINAL} | pr=repo-private-data#1 | delegated=0 {AUTHORED}"
    assert g.delegation_cell_reason(row) is None
    assert g.row_refusals(row) == []


def test_lane_authored_with_diff_cited_is_accepted() -> None:
    row = f"{TERMINAL} | diff=abc1234 | delegated=0 | delegation_reason=lane-authored:no-route"
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    "reason", ["lane-authored", "lane-authored:", "lane-authored:  "]
)
def test_lane_authored_empty_why_is_refused(reason: str) -> None:
    row = (
        f"{TERMINAL} | pr=repo-private-data#1 | delegated=0 delegation_reason={reason}"
    )
    refusal = g.delegation_cell_reason(row)
    assert refusal is not None
    assert "needs a why" in refusal
    assert refusal in g.row_refusals(row)


def test_lane_authored_without_pr_or_diff_is_refused() -> None:
    row = f"{TERMINAL} | delegated=0 delegation_reason=lane-authored:no-route"
    refusal = g.delegation_cell_reason(row)
    assert refusal is not None
    assert "pr= or diff=" in refusal


def test_payload_requires_accounting_and_leaves_historical_rows_untouched() -> None:
    ledger = "ROLLING_WORK_LEDGER.md"
    historical = "2026-09-27T10:00:00Z | TERMINAL | lane=lane-b | friction=none"
    refused = g.refusal_for_payload(TERMINAL, ledger)
    assert refused is not None
    assert "OMN-17427" in refused
    row = TERMINAL + " | delegated=2 runs=a,b"
    assert g.refusal_for_payload(row, ledger) is None
    assert g.refusal_for_state(row, historical + "\n", ledger) is None
    # The historical row would fail if it were submitted as new work.
    assert g.refusal_for_payload(historical, ledger) is not None


@pytest.mark.parametrize(
    "cells", [("delegated=1", "runs=x"), ("actor=script:test", "model=none")]
)
def test_payload_requires_accounting_or_machine_identity(
    cells: tuple[str, str],
) -> None:
    ledger = "ROLLING_WORK_LEDGER.md"
    assert g.refusal_for_payload(TERMINAL, ledger) is not None
    row = TERMINAL + " | " + " | ".join(cells)
    assert g.refusal_for_payload(row, ledger) is None


def test_payload_refuses_empty_lane_authored_why() -> None:
    base = f"{TERMINAL} | pr=repo#1 | delegated=0 "
    refused = g.refusal_for_payload(
        base + "delegation_reason=lane-authored:", "ROLLING_WORK_LEDGER.md"
    )
    assert refused is not None
    assert "needs a why" in refused
    assert g.refusal_for_payload(base + AUTHORED, "ROLLING_WORK_LEDGER.md") is None
