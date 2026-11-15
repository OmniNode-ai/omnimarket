# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TERMINAL running-path cell: an entry point and a line from its latest run."""

from __future__ import annotations

import pytest

from omnimarket.handlers import ledger_row_grammar as g
from omnimarket.handlers import ledger_running_path_cell as running_path_cell

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _rule_off_unless_a_test_sets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(running_path_cell.ENFORCE_FROM_ENV, raising=False)


TS = "2026-11-14T19:33:58Z"
CUTOVER = "2026-11-14T00:00:00Z"
ENFORCE = running_path_cell.ENFORCE_FROM_ENV
BASE = (
    f"{TS} | TERMINAL | lane=lane-a | friction=none | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet | "
    "delegated=0 delegation_reason=no-text-or-code"
)
GOOD_LINE = "2026-11-14T19:30Z node_x_effect tick refused 3 rows"

# A real TERMINAL row of 2026-11-14, kept verbatim except the principal name (renamed): outcome=done with a receipt and a
# host closeout and no cell naming what runs the change.
REAL_ROW_NO_READING = (
    "2026-11-14T19:33:58Z | TERMINAL | lane=fill-ticket-OMN-23215-20261114T1908Z | "
    "principal=dev-lane-principal | ticket=OMN-23215 | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet | friction=none | "
    "closes-CLAIM=2026-11-14T19:32:27Z | outcome=done | host=h053 | "
    "run=rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d | "
    "delegated=0 delegation_reason=no-text-or-code | "
    "req=5eb94e1d-3ffd-4ff2-8ae4-4f6d4af891f1 | via=bus:h200 | remote lane on h053 "
    "ended status=done engine_rc=0; receipt "
    "rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d.json on the launching host; "
    "host closeout: the lane made no worktrees"
)


def omn24535_reasons(row: str) -> list[str]:
    return [reason for reason in g.row_refusals(row) if "OMN-24535" in reason]


@pytest.mark.parametrize("outcome", ["done", "handed-off"])
def test_golden_entry_point_with_line_is_admitted(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    wrong = f"{BASE} | outcome={outcome} | running_path=node_x_effect tick step"
    reasons = omn24535_reasons(wrong)
    assert len(reasons) == 1
    assert "running_line carries no line" in reasons[0]
    row = (
        f"{BASE} | outcome={outcome} | running_path=node_x_effect "
        f"| running_line={GOOD_LINE}"
    )
    assert g.row_refusals(row) == []


def test_golden_none_ci_only_carries_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = (
        f"{BASE} | outcome=done | "
        "running_path=none-ci-only:library-change-no-runtime-path"
    )
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    "suffix",
    [
        "outcome=done",
        "outcome=handed-off",
        "outcome=done running_path=none",
        "outcome=done running_path=tbd",
        "outcome=done running_path=none-ci-only",
        "outcome=done running_path=none-ci-only:x",
        "outcome=done running_path=two words",
        "outcome=done running_path=node_x",
        "outcome=done running_path=node_x running_line=tbd",
        "outcome=done running_path=node_x running_line=node_x",
    ],
)
def test_broken_rows_are_refused_with_the_ticket_mark(
    suffix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = f"{BASE} | {suffix}"
    reasons = g.row_refusals(row)
    flagged = [reason for reason in reasons if "OMN-24535" in reason]
    assert len(reasons) == 1
    assert flagged == reasons


def test_off_by_default_a_done_row_without_cells_is_admitted() -> None:
    assert g.row_refusals(f"{BASE} | outcome=done") == []


def test_a_row_before_the_cutover_is_admitted_and_one_at_or_after_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENFORCE, "2026-11-15T00:00:00Z")
    assert g.row_refusals(f"{BASE} | outcome=done") == []
    at_cutover = BASE.replace(TS, "2026-11-15T00:00:00Z")
    assert len(omn24535_reasons(f"{at_cutover} | outcome=done")) == 1
    later = BASE.replace(TS, "2026-11-15T01:00:00Z")
    assert len(omn24535_reasons(f"{later} | outcome=done")) == 1


def test_a_machine_identity_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = (
        f"{TS} | TERMINAL | lane=lane-a | friction=none | "
        "actor=script:landing-controller | model=none | outcome=done"
    )
    assert g.row_refusals(row) == []


def test_a_non_confirming_outcome_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    assert g.row_refusals(f"{BASE} | outcome=partial") == []


def test_env_one_judges_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENFORCE, "1")
    assert len(omn24535_reasons(f"{BASE} | outcome=done")) == 1
    older = BASE.replace(TS, "2020-01-01T00:00:00Z")
    assert len(omn24535_reasons(f"{older} | outcome=done")) == 1
    row = (
        f"{BASE} | outcome=done | running_path=node_x_effect | running_line={GOOD_LINE}"
    )
    assert g.row_refusals(row) == []


def test_the_real_row_is_refused_and_its_corrected_twin_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    missing = (
        "TERMINAL outcome=done carries no running-path cell (OMN-24535): add "
        + running_path_cell.SHAPE
    )
    assert g.row_refusals(REAL_ROW_NO_READING) == [missing]
    twin = REAL_ROW_NO_READING.replace(
        " | delegated=0",
        " | running_path=none-ci-only:fill-lane-opens-no-pull-request | delegated=0",
    )
    assert g.row_refusals(twin) == []


@pytest.mark.parametrize(
    "cell",
    [
        "outcome=done delegated=0 delegation_reason=no-text-or-code",
        "outcome=handed-off running_path=none",
        "delegated=0 outcome=done",
    ],
)
def test_an_outcome_sharing_its_pipe_cell_with_other_keys_is_still_judged(
    cell: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass a one-token read would leave: the outcome is not its own cell."""
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = f"{TS} | TERMINAL | lane=lane-a | friction=none | actor=claude:sonnet:x | model=claude-sonnet | {cell}"
    assert len(omn24535_reasons(row)) == 1


def test_a_second_non_confirming_outcome_does_not_hide_a_done_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = f"{BASE} | outcome=partial | outcome=done"
    assert len(omn24535_reasons(row)) == 1


def test_a_running_line_in_free_text_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENFORCE, CUTOVER)
    row = f"{BASE} | outcome=done | the lane saw running_path=node_x_effect running_line=ok"
    assert len(omn24535_reasons(row)) == 1
