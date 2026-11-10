# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Dropped test_scoped_release_frees_the_named_prs_and_not_others: requires the holds CLI.
# Dropped test_a_plain_release_still_frees_every_pr: requires the holds CLI.
# Dropped test_a_plain_release_after_a_scoped_one_frees_the_rest: requires the holds CLI.
# Dropped test_scoped_releases_add_up_on_a_pr_list_hold: requires the holds CLI.
# Dropped test_a_comma_list_hold_is_found_by_any_pr_it_names: requires the holds CLI.
# Dropped test_release_verb_writes_a_scoped_row_the_grammar_accepts: requires CLI writes.
# Dropped test_release_verb_refuses_a_bad_scope_and_a_surface_scope: requires CLI validation.
# Dropped test_inbox_keeps_a_hold_a_scoped_release_did_not_end: requires the inbox CLI.
# Dropped test_catchup_lists_a_hold_a_scoped_release_did_not_end: requires the catchup CLI.
# Dropped test_the_reconciler_pairs_a_hold_only_with_a_full_release: requires the reconcile node.
# Rewrote reachable hold/release semantics as direct grammar tests below.
"""Scoped RELEASE rows free only the named PRs; a plain release frees the whole hold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
THREE = "repo#372,repo#386,repo#388"
OTHER = "repo#354"


def stamp(shift: int = 0) -> str:
    return (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(seconds=shift)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


TS = stamp()
HOLD_ID = f"{TS}-lane-a"
REPO_HOLD = (
    f"{TS} | HOLD | lane=lane-a | id={HOLD_ID} | repo=repo | to=lane-b | "
    f"release=operator | ruling={TS} | every open dashboard PR is held"
)
LIST_HOLD_ID = f"{TS}-lane-b"
LIST_HOLD = (
    f"{TS} | HOLD | lane=lane-b | id={LIST_HOLD_ID} | pr=repo#1,repo#2 | to=lane-b | "
    f"until={stamp(3600)} | two PRs"
)


def scoped(hold_id: str, prs: str, lane: str = "lane-b") -> str:
    return f"{TS} | RELEASE | lane={lane} | re={hold_id} | pr={prs} | scoped"


def plain(hold_id: str, lane: str = "lane-b") -> str:
    return f"{TS} | RELEASE | lane={lane} | re={hold_id} | whole hold"


def row(line: str) -> g.Row:
    parsed = g.parse_row(line)
    assert parsed is not None
    return parsed


def test_grammar_accepts_a_scoped_release_and_a_plain_one() -> None:
    assert g.row_refusals(scoped(HOLD_ID, THREE)) == []
    assert g.row_refusals(plain(HOLD_ID)) == []


@pytest.mark.parametrize("bad", ["372", "repo#372,388", "repo#", "repo#372,"])
def test_grammar_refuses_a_malformed_scope_on_a_hold_release(bad: str) -> None:
    reasons = g.row_refusals(scoped(HOLD_ID, bad))
    assert reasons
    assert any("pr=" in reason and "malformed" in reason for reason in reasons)


def test_grammar_leaves_claim_and_surface_releases_alone() -> None:
    claim = f"{TS} | RELEASE | lane=lane-b | re={TS} | pr=not-a-ref | handing over"
    surface = (
        f"{TS} | RELEASE | lane=lane-b | re={TS}-lane-b | surface=slot-1 | pr=repo#3 | "
        "result=PASS | restored=yes"
    )
    assert g.row_refusals(claim) == []
    assert g.row_refusals(surface) == []


def test_grammar_text_names_the_scoped_release() -> None:
    rules = str(g.GRAMMAR["RELEASE"]["rules"])
    assert "SCOPED" in rules
    assert "release_scope()" in rules


def test_release_scope_reads_the_named_prs_only() -> None:
    assert g.release_scope(row(scoped(HOLD_ID, "Repo#372,repo#386"))) == frozenset(
        {"repo#372", "repo#386"}
    )
    assert g.release_scope(row(plain(HOLD_ID))) is None
    claim = f"{TS} | RELEASE | lane=lane-b | re={TS} | pr=repo#9 | handing over"
    assert g.release_scope(row(claim)) is None
    surface = f"{TS} | RELEASE | lane=lane-b | re={TS}-lane-b | surface=slot | pr=repo#3 | result=PASS"
    assert g.release_scope(row(surface)) is None


def test_an_unreadable_scope_frees_nothing_and_never_widens() -> None:
    unreadable = row(scoped(HOLD_ID, "372"))
    assert g.release_scope(unreadable) == frozenset()
    released, freed = g.hold_release_state(row(REPO_HOLD), [unreadable])
    assert released is False
    assert freed == frozenset()


def test_hold_state_repo_wide_hold_survives_every_scoped_release() -> None:
    hold = row(REPO_HOLD)
    released, freed = g.hold_release_state(hold, [row(scoped(HOLD_ID, THREE))])
    assert released is False
    assert freed == frozenset(THREE.split(","))
    assert g.hold_release_state(hold, [row(plain(HOLD_ID))])[0] is True
    other_hold = row(scoped(f"{TS}-lane-b", THREE))
    assert g.hold_release_state(hold, [other_hold]) == (False, frozenset())


def test_hold_state_pr_list_hold_ends_once_its_list_is_covered() -> None:
    hold = row(LIST_HOLD)
    first = row(scoped(LIST_HOLD_ID, "repo#1"))
    second = row(scoped(LIST_HOLD_ID, "repo#2"))
    assert g.hold_release_state(hold, [first]) == (False, frozenset({"repo#1"}))
    assert g.hold_release_state(hold, [first, second])[0] is True
    assert (
        g.hold_release_state(hold, [row(scoped(LIST_HOLD_ID, "repo#1,repo#2"))])[0]
        is True
    )
    assert g.hold_release_state(hold, [row(scoped(LIST_HOLD_ID, "repo#7"))])[0] is False


def _is_held(hold: g.Row, releases: list[g.Row], pr: str) -> bool:
    released, freed = g.hold_release_state(hold, releases)
    scope = hold.token("pr")
    applies = (
        pr in scope.split(",") if scope else pr.split("#", 1)[0] == hold.token("repo")
    )
    return applies and not released and pr not in freed


def test_scoped_release_frees_named_prs_and_keeps_other_prs_held() -> None:
    hold = row(REPO_HOLD)
    for pr in (*THREE.split(","), OTHER):
        assert _is_held(hold, [], pr), f"{pr} is held before the release"
    releases = [row(scoped(HOLD_ID, THREE))]
    for pr in THREE.split(","):
        assert not _is_held(hold, releases, pr), f"{pr} is freed by the scoped release"
    assert _is_held(hold, releases, OTHER)
    assert _is_held(hold, releases, "repo#1")
    released, freed = g.hold_release_state(hold, releases)
    assert released is False
    assert freed == frozenset(THREE.split(","))


def test_plain_release_frees_every_pr() -> None:
    hold = row(REPO_HOLD)
    assert _is_held(hold, [], OTHER)
    releases = [row(plain(HOLD_ID))]
    for pr in (*THREE.split(","), OTHER):
        assert not _is_held(hold, releases, pr)
    assert g.hold_release_state(hold, releases) == (True, frozenset())


def test_plain_release_after_scoped_release_frees_the_rest() -> None:
    hold = row(REPO_HOLD)
    releases = [row(scoped(HOLD_ID, THREE))]
    assert _is_held(hold, releases, OTHER)
    releases.append(row(plain(HOLD_ID)))
    assert not _is_held(hold, releases, OTHER)
    assert g.hold_release_state(hold, releases) == (True, frozenset(THREE.split(",")))


def test_scoped_releases_add_up_on_a_pr_list_hold() -> None:
    hold = row(LIST_HOLD)
    assert _is_held(hold, [], "repo#1")
    assert _is_held(hold, [], "repo#2")
    releases = [row(scoped(LIST_HOLD_ID, "repo#1"))]
    assert not _is_held(hold, releases, "repo#1")
    assert _is_held(hold, releases, "repo#2")
    releases.append(row(scoped(LIST_HOLD_ID, "repo#2")))
    assert g.hold_release_state(hold, releases) == (
        True,
        frozenset({"repo#1", "repo#2"}),
    )


def test_comma_list_hold_applies_to_each_named_pr() -> None:
    hold = row(LIST_HOLD)
    assert _is_held(hold, [], "repo#1")
    assert _is_held(hold, [], "repo#2")
    assert not _is_held(hold, [], "repo#3")


def test_scoped_release_payload_is_accepted_and_bad_scope_is_refused() -> None:
    ledger = "ROLLING_WORK_LEDGER.md"
    valid = scoped(HOLD_ID, THREE)
    assert g.refusal_for_payload(valid, ledger) is None
    reason = g.refusal_for_payload(scoped(HOLD_ID, "372"), ledger)
    assert reason is not None
    assert "pr=" in reason
    assert "malformed" in reason
