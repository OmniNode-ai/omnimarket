# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Rewrote test_hold_e2e_cli_accepts_structured_hold_and_refuses_free_text; dropped retired append command I/O.
# Rewrote test_operator_hold_e2e_cli_appends; dropped retired append command I/O.
"""Structured HOLD and free-text hold grammar with synthetic rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
TS = "2026-09-26T02:00:00Z"
LEDGER = "docs/tracking/ROLLING_WORK_LEDGER.md"
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "ledger_row_grammar"
    / "hold_grammar_rows.txt"
)


def _fixture_rows() -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {
        "refused": [],
        "allowed": [],
        "hold-refused": [],
    }
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        heading = line.removeprefix("## ")
        group, why = heading.split(": ", 1)
        groups[group].append((why, lines[index + 1]))
    return groups


FIXTURE_ROWS = _fixture_rows()


def _row_type(row: str) -> str:
    parsed = g.parse_row(row)
    assert parsed is not None
    return parsed.row_type


@pytest.mark.parametrize(
    ("why", "row"),
    FIXTURE_ROWS["refused"],
    ids=[why for why, _ in FIXTURE_ROWS["refused"]],
)
def test_refused_fixture_rows_free_text_hold(why: str, row: str) -> None:
    del why
    assert g.hold_instruction_refusal(row, _row_type(row)) is not None
    refusal = g.refusal_for_payload(row, LEDGER)
    assert refusal is not None
    assert "HOLD row" in refusal


@pytest.mark.parametrize(
    ("why", "row"),
    FIXTURE_ROWS["allowed"],
    ids=[why for why, _ in FIXTURE_ROWS["allowed"]],
)
def test_allowed_fixture_rows_are_not_hold_instructions(why: str, row: str) -> None:
    refusal = g.hold_instruction_refusal(row, _row_type(row))
    assert refusal is None
    hold_reasons = [
        reason
        for reason in g.row_refusals(row)
        if "release condition" in reason or "free text" in reason
    ]
    assert hold_reasons == []
    if _row_type(row) == "HOLD":
        assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    ("why", "row"),
    FIXTURE_ROWS["hold-refused"],
    ids=[why for why, _ in FIXTURE_ROWS["hold-refused"]],
)
def test_hold_refused_fixture_rows_without_release_condition(
    why: str, row: str
) -> None:
    del why
    assert any("release condition" in reason for reason in g.row_refusals(row))


def _status(text: str, *, pr: str = "repo#1") -> str:
    return f"{TS} | STATUS | lane=lane-a | pr={pr} | {text}"


@pytest.mark.parametrize(
    "phrase",
    [
        "do not land repo#1",
        "don't merge this",
        "don\u2019t land that",
        "must not land alone",
        "mustn't merge it",
        "should not land both",
        "may not merge either",
        "shall not land the PR",
        "not to merge PR repo#1",
        "do not be landed",
        "not be merged",
        "hold off landing",
        "hold off on merging",
        "never land it",
        "never merge this",
        "never land the PR",
        "never merge PR repo#1",
        "never land repo#1",
        "never merge /pull/2",
        "land after repo#2",
        "lands only after PR repo#2",
        "landing must happen before repo#2",
        "merge alongside repo#2",
        "merges together with PR repo#2",
        "land in order",
        "lands-in-order",
        "landing-in-order",
        "merge in order",
        "order matters",
        "landing order: repo#2 first",
        "do not arm or merge repo#1",
        "do not arm/merge it",
        "the lane's PR can't wait; do not land repo#1",
    ],
)
def test_refused_synthetic_hold_instruction_patterns(phrase: str) -> None:
    refusal = g.hold_instruction_refusal(_status(phrase), "STATUS")
    assert refusal is not None
    assert "STATUS" in refusal
    assert "free text cannot hold a PR because nothing can release it" in refusal
    assert "HOLD row" in refusal
    assert "after=<repo>#<n> or until=<UTC ts> or proof=<row id>" in refusal
    assert "double quotes" in refusal
    assert "matched" in refusal


@pytest.mark.parametrize(
    "text",
    [
        "never arm",
        "do not arm",
        "ARM-OFF",
        "never arm; land by REST squash once green",
        "Never merge, arm or rerun",
        "never arm/merge/rerun CI",
        "Never merge, ready-flip, release, or touch",
        "the do-not-merge label remains",
        'report says "do not land repo#1"',
        "report says “do not land repo#1”",
        "report says `do not land repo#1`",
        "report says 'do not land repo#1', which the lane's audit found stale",
        "do not arm or merge",
        "do not arm/merge",
        "do not arm, ready-flip, or merge",
        "Codex must not merge/arm/relabel",
        "Do not merge, arm or rerun",
        "merge conflicts with repo#2 resolved",
        "no runtime merge this cycle; after repo#2 lands the start is separate",
        "landed step (4) of the landing order in MSG ledger:2975",
    ],
)
def test_allowed_synthetic_wording(text: str) -> None:
    assert g.hold_instruction_refusal(_status(text), "STATUS") is None


def test_allowed_do_not_land_without_a_pr_reference() -> None:
    assert (
        g.hold_instruction_refusal(_status("do not land", pr="none"), "STATUS") is None
    )


@pytest.mark.parametrize("row_type", ["RULING", "OPERATOR-CONSENT"])
def test_allowed_exempt_authority_rows(row_type: str) -> None:
    row = f'{TS} | {row_type} | lane=lane-a | "do not land repo#1"'
    assert g.hold_instruction_refusal(row, row_type) is None


def test_refused_continuation_line_hold_instruction_through_payload() -> None:
    row = f"{TS} | MSG | from=lane-a | to=all | id={TS}-lane-a | report follows"
    refusal = g.refusal_for_payload(row + "\ndo not land repo#1", LEDGER)
    assert refusal is not None
    assert "line 1" in refusal
    assert "HOLD row" in refusal


def test_refused_single_line_hold_instruction_is_reported_once_by_both_entry_points() -> (
    None
):
    row = _status("do not land repo#1")
    reasons = g.row_refusals(row)
    assert sum("free text cannot hold a PR" in reason for reason in reasons) == 1
    refusal = g.refusal_for_payload(row, LEDGER)
    assert refusal is not None
    assert refusal.count("free text cannot hold a PR") == 1


def test_allowed_multiline_quoted_hold_instruction() -> None:
    row = f'{TS} | STATUS | lane=lane-a | report says "do not land\n'
    assert g.refusal_for_payload(row + 'repo#1"', LEDGER) is None


def test_allowed_past_tense_ordering_report() -> None:
    row = _status("repo#2 merged after repo#1")
    assert g.hold_instruction_refusal(row, "STATUS") is None


def _hold(*cells: str) -> str:
    return f"{TS} | HOLD | lane=lane-a | id={TS}-lane-a | pr=repo#1 | " + " | ".join(
        cells
    )


def test_hold_refused_when_release_condition_is_missing() -> None:
    reasons = g.row_refusals(_hold("reason=waiting"))
    assert any(
        "release condition" in reason
        and "after=" in reason
        and "until=" in reason
        and "proof=" in reason
        for reason in reasons
    )


@pytest.mark.parametrize(
    "release",
    [
        "after=repo#1,repo-infra#2",
        "until=2026-09-26T03:00:00Z",
        "proof=2026-09-26T01:00:00Z-lane-a",
        "proof=2026-09-26T01:00:00Z,2026-09-26T01:01:00Z-lane-b",
    ],
)
def test_hold_allowed_with_valid_release_condition(release: str) -> None:
    assert g.row_refusals(_hold(release)) == []


@pytest.mark.parametrize(
    ("cell", "fragment"),
    [
        ("after=soon", "after="),
        ("until=tomorrow", "until="),
        ("proof=ledger:5400", "proof="),
    ],
)
def test_hold_refused_when_release_condition_is_malformed(
    cell: str, fragment: str
) -> None:
    reasons = g.row_refusals(_hold(cell))
    assert any(fragment in reason for reason in reasons)


def test_hold_refused_when_pr_scope_is_malformed() -> None:
    row = f"{TS} | HOLD | lane=lane-a | id={TS}-lane-a | pr=repo | after=repo#1"
    assert any("pr=" in reason for reason in g.row_refusals(row))


def test_hold_grammar_data_names_release_fields_and_version_two() -> None:
    assert g.GRAMMAR_VERSION == "ledger-row-grammar/2"
    optional = g.GRAMMAR["HOLD"]["optional"]
    assert isinstance(optional, list)
    assert {"after", "until", "proof"} <= set(optional)
    rules = g.GRAMMAR["HOLD"]["rules"]
    assert isinstance(rules, list)
    assert any("release condition" in str(rule) for rule in rules)


OPERATOR_RULING = "ruling=2026-09-24T15:55:55Z-lane-a"


@pytest.mark.parametrize(
    "ruling",
    [
        OPERATOR_RULING,
        "ruling=2026-09-24T23:18:03Z",
        "ruling=2026-09-23T18:13:52Z-lane-a,2026-09-24T23:18:03Z-lane-b",
    ],
)
def test_operator_hold_allowed_with_release_operator_and_ruling(ruling: str) -> None:
    assert (
        g.row_refusals(_hold("release=operator", ruling, "reason=standing exclusion"))
        == []
    )


def test_operator_hold_refused_without_ruling() -> None:
    reasons = g.row_refusals(_hold("release=operator", "reason=standing exclusion"))
    assert any("ruling=" in reason for reason in reasons), reasons


def test_operator_hold_refused_with_malformed_ruling() -> None:
    reasons = g.row_refusals(_hold("release=operator", "ruling=ledger:1960"))
    assert any("ruling=" in reason and "malformed" in reason for reason in reasons), (
        reasons
    )


@pytest.mark.parametrize("value", ["lane-a", "anyone", "Operator ", "operator,lane-a"])
def test_release_other_than_operator_is_refused(value: str) -> None:
    reasons = g.row_refusals(_hold(f"release={value}", OPERATOR_RULING))
    assert any("release=" in reason for reason in reasons), reasons


def test_ruling_alone_is_not_a_release_condition() -> None:
    reasons = g.row_refusals(_hold(OPERATOR_RULING))
    assert any("release condition" in reason for reason in reasons), reasons


def test_missing_release_condition_message_names_the_operator_form() -> None:
    reasons = g.row_refusals(_hold("reason=waiting"))
    assert any(
        "release condition" in reason
        and "release=operator" in reason
        and "ruling=" in reason
        for reason in reasons
    ), reasons


def test_hold_grammar_data_names_the_operator_release_form() -> None:
    optional = g.GRAMMAR["HOLD"]["optional"]
    assert isinstance(optional, list)
    assert {"release", "ruling"} <= set(optional)
    rules = g.GRAMMAR["HOLD"]["rules"]
    assert isinstance(rules, list)
    assert any(
        "release=operator" in str(rule) and "ruling=" in str(rule) for rule in rules
    )


def test_hold_payload_accepts_structured_hold_and_refuses_free_text() -> None:
    hold = _hold("after=repo#0", "reason=waiting")
    assert g.refusal_for_payload(hold, LEDGER) is None
    message = f"{TS} | MSG | from=lane-a | to=all | id={TS}-lane-a | do not land repo#1"
    refused = g.refusal_for_payload(message, LEDGER)
    assert refused is not None
    assert "HOLD row" in refused


def test_operator_hold_payload_requires_ruling() -> None:
    hold = _hold(
        "release=operator", OPERATOR_RULING, "reason=operator standing exclusion"
    )
    assert g.refusal_for_payload(hold, LEDGER) is None
    refused = g.refusal_for_payload(_hold("release=operator"), LEDGER)
    assert refused is not None
    assert "ruling=" in refused
