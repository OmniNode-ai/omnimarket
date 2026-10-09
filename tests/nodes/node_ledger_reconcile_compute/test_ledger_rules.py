# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure ledger parsing, pairing, evidence and row-building rules (OMN-20677)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from omnimarket.nodes.node_ledger_reconcile_compute.handlers import (
    decision,
    evidence,
    findings,
    ledger_rows,
    pairing,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 6, 22, 0, tzinfo=UTC)
LEDGER_NAME = "ROLLING_WORK_LEDGER.md"


def ts(text: str) -> datetime:
    parsed = ledger_rows.parse_ts(text)
    assert parsed is not None, text
    return parsed


def test_parse_ts_full_and_minute_precision_and_date_only() -> None:
    assert ts("2026-08-31T23:15:00Z") == datetime(2026, 8, 31, 23, 15, tzinfo=UTC)
    assert ts("2026-08-29T03:45Z") == datetime(2026, 8, 29, 3, 45, tzinfo=UTC)
    assert ts("2026-08-29") == datetime(2026, 8, 29, tzinfo=UTC)


def test_parse_ts_rejects_junk() -> None:
    assert ledger_rows.parse_ts("2026-07-13T12:0xZ") is None
    assert ledger_rows.parse_ts("not a date") is None


CLAIM_ROW = (
    "| 2026-08-31T23:15:00Z | wave2-omn17350-check8 | OMN-17350 | CLAIM | "
    "**Owning OMN-17350.** Scope: omninode_infra#1122 only. |"
)


TERMINAL_ROW = (
    "| 2026-09-01T08:37:14Z | wave2-omn17350-check8 | OMN-17350 | TERMINAL | "
    "**LATE TERMINAL** — closes the CLAIM row at 2026-08-31T23:15:00Z. "
    "omninode_infra#1122 MERGED squash 6f1b394771575c0f. |"
)


def test_parse_pipe_row_claim() -> None:
    row = ledger_rows.parse_pipe_row(CLAIM_ROW, "t:1")
    assert isinstance(row, ledger_rows.Row)
    assert row.kind == "CLAIM"
    assert row.lane == "wave2-omn17350-check8"
    assert row.tickets == frozenset({"OMN-17350"})
    assert row.ts == datetime(2026, 8, 31, 23, 15, tzinfo=UTC)
    assert "omninode_infra#1122" in row.body


def test_parse_pipe_row_terminal_with_qualifier_suffix() -> None:
    drifted = CLAIM_ROW.replace("| CLAIM |", "| TERMINAL (recorded, not owned) |")
    row = ledger_rows.parse_pipe_row(drifted, "t:2")
    assert isinstance(row, ledger_rows.Row)
    assert row.kind == "TERMINAL"


def test_parse_pipe_row_ignores_other_row_classes() -> None:
    note = CLAIM_ROW.replace("| CLAIM |", "| NOTE |")
    assert ledger_rows.parse_pipe_row(note, "t:3") is None
    attention = CLAIM_ROW.replace("| CLAIM |", "| NEEDS-ATTENTION |")
    assert ledger_rows.parse_pipe_row(attention, "t:4") is None


def test_parse_pipe_row_ignores_registry_tables() -> None:
    reg = "| OMN-15725 pin bump | `ticket-dedupe-0808` | claimed | none | 2026-08-08T00:08:00Z |"
    assert ledger_rows.parse_pipe_row(reg, "t:5") is None


def test_parse_pipe_row_reports_claim_shaped_but_unparseable() -> None:
    weird = "| 2026-08-31T23:15:00Z | lane | body that mentions a CLAIM but has no type cell |"
    outcome = ledger_rows.parse_pipe_row(weird, "t:6")
    assert isinstance(outcome, str)
    assert "t:6" in outcome


def test_parse_pipe_row_body_with_embedded_pipes_survives() -> None:
    row = ledger_rows.parse_pipe_row(
        "| 2026-08-31T23:15:00Z | lane-x | OMN-1 | CLAIM | left | right | tail |",
        "t:7",
    )
    assert isinstance(row, ledger_rows.Row)
    assert row.body == "left | right | tail"


def test_parse_heading_row_claim_with_fallback_ts() -> None:
    fallback = ts("2026-08-31")
    rows = ledger_rows.parse_heading_row(
        "### CLAIM — OMN-16919 — cross-source ledger reconciliation", "a:1", fallback
    )
    assert [r.kind for r in rows] == ["CLAIM"]
    assert rows[0].tickets == frozenset({"OMN-16919"})
    assert rows[0].ts == fallback
    assert rows[0].lane is None


def test_parse_heading_row_combined_claim_terminal() -> None:
    rows = ledger_rows.parse_heading_row(
        "## 2026-08-29T01:23:43Z — OMN-14256: secret provisioned — CLAIM+TERMINAL",
        "a:2",
        None,
    )
    assert sorted(r.kind for r in rows) == ["CLAIM", "TERMINAL"]
    assert all(r.ts == ts("2026-08-29T01:23:43Z") for r in rows)


def rows_of(*lines: str) -> list[ledger_rows.Row]:
    parsed = []
    for i, line in enumerate(lines):
        row = ledger_rows.parse_pipe_row(line, f"p:{i}")
        assert isinstance(row, ledger_rows.Row), line
        parsed.append(row)
    return parsed


def test_pair_terminal_closes_same_lane_claim() -> None:
    assert pairing.pair_rows(rows_of(CLAIM_ROW, TERMINAL_ROW)) == []


def test_pair_terminal_before_claim_does_not_close() -> None:
    early_terminal = TERMINAL_ROW.replace(
        "2026-09-01T08:37:14Z", "2026-08-30T00:00:00Z"
    )
    open_claims = pairing.pair_rows(rows_of(CLAIM_ROW, early_terminal))
    assert len(open_claims) == 1


def test_pair_ticket_disjoint_does_not_close() -> None:
    other = TERMINAL_ROW.replace("| OMN-17350 |", "| OMN-99999 |")
    assert len(pairing.pair_rows(rows_of(CLAIM_ROW, other))) == 1


def test_pair_explicit_ts_binding_beats_lifo() -> None:
    old_claim = CLAIM_ROW
    new_claim = CLAIM_ROW.replace("2026-08-31T23:15:00Z", "2026-09-01T07:00:00Z")
    # TERMINAL cites the OLD claim's timestamp; LIFO alone would bind the new one.
    open_claims = pairing.pair_rows(rows_of(old_claim, new_claim, TERMINAL_ROW))
    assert [c.ts for c in open_claims] == [ts("2026-09-01T07:00:00Z")]


def test_pair_lifo_when_no_explicit_binding() -> None:
    old_claim = CLAIM_ROW
    new_claim = CLAIM_ROW.replace("2026-08-31T23:15:00Z", "2026-09-01T07:00:00Z")
    plain_terminal = "| 2026-09-01T08:37:14Z | wave2-omn17350-check8 | OMN-17350 | TERMINAL | done. |"
    open_claims = pairing.pair_rows(rows_of(old_claim, new_claim, plain_terminal))
    assert [c.ts for c in open_claims] == [ts("2026-08-31T23:15:00Z")]


def test_extract_evidence_pr_refs_aliases_and_unknowns() -> None:
    clones = {"omnibase_infra"}
    ev = evidence.extract_evidence(
        "landed omnibase_infra#2632 and occ#7811; mystery ref foo#1",
        clones,
        {"occ": "onex_change_control"},
    )
    resolved = {(p.repo, p.number) for p in ev.prs}
    assert ("omnibase_infra", 2632) in resolved
    assert ("onex_change_control", 7811) in resolved
    assert any(p.repo is None and p.raw_repo == "foo" for p in ev.prs)


def test_extract_evidence_shas_require_hex_letter() -> None:
    ev = evidence.extract_evidence(
        "squash 6f1b394771575c0f; run id 31455796265", (), {}
    )
    assert [s.sha for s in ev.shas] == ["6f1b394771575c0f"]


def test_extract_evidence_branches() -> None:
    ev = evidence.extract_evidence(
        "branch jonah/omn-17466-reconciler pushed",
        (),
        {},
        ("jonah", "codex", "promotion"),
    )
    assert ev.branches == ["jonah/omn-17466-reconciler"]


def pr(repo: str | None, number: int, state: str, sha: str = "") -> evidence.PrHandle:
    handle = evidence.PrHandle(raw_repo=repo or "x", repo=repo, number=number)
    handle.state = state
    handle.merge_sha = sha
    return handle


def test_verdict_all_merged_is_completed() -> None:
    ev = evidence.Evidence(
        prs=[
            pr("omnibase_infra", 1, "MERGED", "abc123"),
            pr("omnimarket", 2, "MERGED", "def456"),
        ]
    )
    verdict, detail = evidence.verdict_for(ev)
    assert verdict == evidence.COMPLETED
    assert "omnibase_infra#1 MERGED" in detail


def test_verdict_any_open_or_closed_is_orphaned() -> None:
    for state in ("OPEN", "CLOSED"):
        ev = evidence.Evidence(
            prs=[pr("omnibase_infra", 1, "MERGED"), pr("omnimarket", 2, state)]
        )
        verdict, detail = evidence.verdict_for(ev)
        assert verdict == evidence.ORPHANED
        assert state in detail


def test_verdict_lookup_failure_degrades_to_unknown_not_completed() -> None:
    ev = evidence.Evidence(
        prs=[pr("omnibase_infra", 1, "MERGED"), pr("omnimarket", 2, "LOOKUP_FAILED")]
    )
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_verdict_only_unresolvable_repo_alias_is_unknown() -> None:
    ev = evidence.Evidence(prs=[pr(None, 1, "UNVERIFIED")])
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_verdict_sha_only_all_landed_is_completed() -> None:
    sha = evidence.ShaHandle(sha="a" * 12)
    sha.found_in, sha.landed = "registry_root", True
    assert evidence.verdict_for(evidence.Evidence(shas=[sha]))[0] == evidence.COMPLETED


def test_verdict_sha_only_unresolved_is_unknown_never_completed() -> None:
    found = evidence.ShaHandle(sha="a" * 12)
    found.found_in, found.landed = "registry_root", True
    missing = evidence.ShaHandle(sha="b" * 12)
    assert (
        evidence.verdict_for(evidence.Evidence(shas=[found, missing]))[0]
        == evidence.UNKNOWN
    )


def test_verdict_sha_present_but_unlanded_is_unknown() -> None:
    sha = evidence.ShaHandle(sha="c" * 12)
    sha.found_in, sha.landed = "omnibase_core", False
    assert evidence.verdict_for(evidence.Evidence(shas=[sha]))[0] == evidence.UNKNOWN


def test_verdict_no_handles_is_unknown() -> None:
    assert evidence.verdict_for(evidence.Evidence())[0] == evidence.UNKNOWN


def claim_row() -> ledger_rows.Row:
    row = ledger_rows.parse_pipe_row(CLAIM_ROW, "t:1")
    assert isinstance(row, ledger_rows.Row)
    return row


def test_bind_excludes_pr_ticket_bound_to_other_work() -> None:
    # A fence-check citation of a peer lane's PR must never auto-close this
    # claim: title cites a different ticket than the claim's.
    handle = pr("omnibase_infra", 3062, "MERGED", "3f10ee5e")
    handle.title = "fix(OMN-17288): unrelated peer work"
    handle.merged_at = "2026-09-01T09:51:17Z"
    ev = evidence.Evidence(prs=[handle])
    evidence.bind_evidence(claim_row(), ev)
    assert handle.bound is False
    verdict, detail = evidence.verdict_for(ev)
    assert verdict == evidence.UNKNOWN
    assert "belongs to other work" in detail


def test_bind_excludes_pr_merged_before_the_claim_even_when_its_ticket_matches() -> (
    None
):
    # OMN-17573, ledger:5750 (a): lab-5090-qwen38-27b-swap-1330 claimed at
    # 13:33:10Z and was auto-closed on omnibase_infra#3681, merged 12:30:04Z,
    # a PR its scope cited as a precondition. The ticket match used to skip
    # the time guard; a PR that landed before the claim is never its work.
    handle = pr("omninode_infra", 1109, "MERGED", "20e02ac1")
    handle.title = "fix(OMN-17350): same ticket, earlier work"
    handle.merged_at = "2026-08-31T21:44:51Z"  # before the 23:15 claim stamp
    ev = evidence.Evidence(prs=[handle])
    evidence.bind_evidence(claim_row(), ev)
    assert handle.bound is False
    assert handle.exclude_reason == "merged before the claim was made"
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_bind_keeps_a_same_ticket_pr_merged_after_the_claim() -> None:
    handle = pr("omninode_infra", 1122, "MERGED", "6f1b3947")
    handle.title = "fix(OMN-17350): the claimed work itself"
    handle.merged_at = "2026-09-01T08:30:00Z"
    ev = evidence.Evidence(prs=[handle])
    evidence.bind_evidence(claim_row(), ev)
    assert handle.bound is True
    assert evidence.verdict_for(ev)[0] == evidence.COMPLETED


def test_bind_excludes_a_same_ticket_commit_made_before_the_claim() -> None:
    early = evidence.ShaHandle(sha="a" * 12)
    early.found_in, early.landed = "omnimarket", True
    early.msg_tickets = frozenset({"OMN-17350"})
    early.committer_ts = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    ev = evidence.Evidence(shas=[early])
    evidence.bind_evidence(claim_row(), ev)
    assert early.bound is False
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_bind_time_guard_excludes_untitled_pr_merged_before_claim() -> None:
    # No ticket on either side to bind with: a PR merged BEFORE the claim
    # existed cannot be its deliverable.
    handle = pr("omnibase_infra", 2305, "MERGED", "9c349c1c")
    handle.title = "some legacy title with no ticket reference"
    handle.merged_at = "2026-07-14T04:18:39Z"
    ev = evidence.Evidence(prs=[handle])
    evidence.bind_evidence(claim_row(), ev)
    assert handle.bound is False
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_bind_sha_message_ticket_and_time_guards() -> None:
    other = evidence.ShaHandle(sha="d" * 12)
    other.found_in, other.landed = "omnimarket", True
    other.msg_tickets = frozenset({"OMN-11111"})
    stale = evidence.ShaHandle(sha="e" * 12)
    stale.found_in, stale.landed = "omnimarket", True
    stale.committer_ts = datetime(2026, 8, 1, tzinfo=UTC)
    ours = evidence.ShaHandle(sha="f" * 12)
    ours.found_in, ours.landed = "omnimarket", True
    ours.msg_tickets = frozenset({"OMN-17350"})
    ev = evidence.Evidence(shas=[other, stale, ours])
    evidence.bind_evidence(claim_row(), ev)
    assert (other.bound, stale.bound, ours.bound) == (False, False, True)
    assert evidence.verdict_for(ev)[0] == evidence.COMPLETED


def test_parse_pipe_row_kind_cell_carries_its_own_body() -> None:
    row = ledger_rows.parse_pipe_row(
        "| 2026-08-14T01:00Z | `secret-scan-repair-0813` | CLAIM — OMN-16045 "
        "TruffleHog false-positive repair |",
        "t:11",
    )
    assert isinstance(row, ledger_rows.Row)
    assert row.kind == "CLAIM"
    assert row.lane == "secret-scan-repair-0813"
    assert "OMN-16045" in row.tickets
    assert row.body.startswith("OMN-16045")


def test_parse_pipe_row_combined_claim_terminal_is_self_closed() -> None:
    assert (
        ledger_rows.parse_pipe_row(
            "| 2026-08-26T14:47:35Z | meeting-calc-truncation | CLAIM→TERMINAL "
            "OMN-16630 fix landed |",
            "t:12",
        )
        is None
    )


def test_parse_pipe_row_bold_wrapped_kind_cell() -> None:
    row = ledger_rows.parse_pipe_row(
        "| 2026-08-30 12:40Z | `omn16991-landing-verification` | OMN-16991 | "
        "**TERMINAL — ticket flipped to Done.** |",
        "t:13",
    )
    assert isinstance(row, ledger_rows.Row)
    assert row.kind == "TERMINAL"


def make_finding(verdict: str) -> findings.Finding:
    claim = ledger_rows.parse_pipe_row(CLAIM_ROW, "t:1")
    assert isinstance(claim, ledger_rows.Row)
    return findings.Finding(
        claim,
        9.5,
        verdict,
        "omninode_infra#1122 MERGED x merge 6f1b3947",
        evidence.Evidence(),
    )


def test_built_terminal_row_parses_and_closes_the_claim() -> None:
    finding = make_finding(evidence.COMPLETED)
    built = findings.build_terminal_row(finding, NOW)
    # OMN-19256: the reconciler writes the canonical timestamp-led shape, which
    # the ledger row grammar admits; the bar-led shape is refused on append.
    assert ledger_rows.parse_pipe_row(built, "t:9") is None
    terminal = ledger_rows.parse_ts_row(built, "t:9")
    assert isinstance(terminal, ledger_rows.Row)
    assert terminal.kind == "TERMINAL"
    assert findings.AUTO_CLOSE_MARK in terminal.body
    claim = cast(ledger_rows.Row, ledger_rows.parse_pipe_row(CLAIM_ROW, "t:1"))
    assert pairing.pair_rows([claim, terminal]) == []


def test_built_attention_row_is_not_claim_shaped_and_not_unparseable() -> None:
    finding = make_finding(evidence.ORPHANED)
    built = findings.build_attention_row(finding, NOW)
    assert findings.ATTENTION_MARK in built
    # The attention flag is a STATUS row (OMN-19256): neither a Row nor an
    # unparseable-report string in either parser, and it must NOT close the claim.
    assert ledger_rows.parse_pipe_row(built, "t:10") is None
    assert ledger_rows.parse_ts_row(built, "t:10") is None


def test_already_flagged_matches_identity_across_texts() -> None:
    finding = make_finding(evidence.COMPLETED)
    built = findings.build_terminal_row(finding, NOW)
    claim = finding.claim
    assert findings.already_flagged(claim, ["irrelevant", built])
    assert not findings.already_flagged(claim, ["no marker here"])


TOKEN_CLAIM = (
    "2026-09-20T10:05:00Z | CLAIM | lane=token-lane | actor=claude:opus5:subagent "
    "| ticket=OMN-101 | closed by its LCT1 token | est ~1 lane-hours; displaces nothing"
)


def both_shapes_fixture(digest: str) -> list[str]:
    """Line numbers matter: the close references below cite them."""
    return [
        "# Rolling work ledger",  # 1
        "",  # 2
        "| 2026-09-20T10:00:00Z | pipe-lane | OMN-100 | CLAIM | pipe-led, closed by lane |",  # 3
        TOKEN_CLAIM,  # 4
        "2026-09-20T10:10:00Z | CLAIM | lane=line-lane | actor=x | ticket=OMN-102 | closed by a ledger line",  # 5
        "2026-09-20T10:15:00Z | CLAIM | lane=closeout-lane | ticket=OMN-103 | closed by a same-lane CLOSEOUT",  # 6
        "2026-09-20T10:20:00Z | CLAIM | lane=dead-lane | ticket=OMN-104 | superseded by another lane",  # 7
        "2026-09-20T10:25:00Z | CLAIM | lane=named-lane | ticket=OMN-105 | closed by a lane= reference",  # 8
        "2026-09-20T10:30:00Z | OMN-106 | CLAIM | lane=open-lane | ticket=OMN-106 | never closed",  # 9
        "2026-09-20T10:35:00Z | barless-lane | OMN-107 | CLAIM | §0a order with no leading bar, never closed",  # 10
        "2026-09-20T10:40:00Z | CLAIM-AMEND | lane=open-lane | amends ledger:9, opens nothing",  # 11
        "| 2026-09-20T11:00:00Z | pipe-lane | OMN-100 | TERMINAL | done. |",  # 12
        # The token's line field (77) is deliberately wrong, as after a roll:
        # only the row digest can bind it.
        f"2026-09-20T11:05:00Z | TERMINAL | lane=token-lane | ticket=OMN-101 | closes-CLAIM=LCT1-999-77-{digest}-2026-09-20T10:05:00Z | outcome=merged",  # 13
        "2026-09-20T11:10:00Z | TERMINAL | lane=line-lane | ticket=OMN-102 | closes-CLAIM=ledger:5 | outcome=merged",  # 14
        "2026-09-20T11:15:00Z | CLOSEOUT | lane=closeout-lane | ticket=OMN-103 | outcome=report delivered",  # 15
        "2026-09-20T11:20:00Z | CLAIM | lane=taker-lane | ticket=OMN-104 | supersedes-claim=ledger:7 | taking over, never closed",  # 16
        "2026-09-20T11:25:00Z | STATUS | lane=someone | ticket=OMN-105 | closes-CLAIM=lane=named-lane (CLAIM appended 2026-09-20T10:25:00Z)",  # 17
        "2026-09-20T11:30:00Z | TERMINAL | lane=wrong-lane | ticket=OMN-999 | closes-CLAIM=ledger:9 | cites an unrelated claim",  # 18
        "2026-09-20T11:35:00Z | NOTE | lane=quoter | quoting a peer row: | CLAIM | lane=ghost |",  # 19
    ]


@pytest.fixture
def both_shapes() -> ledger_rows.ParseResult:
    digest = pairing.claim_digest_function()(TOKEN_CLAIM)
    return ledger_rows.load_rows(
        (LEDGER_NAME, "\n".join(both_shapes_fixture(digest))), []
    )


def test_both_row_shapes_parse_and_pair_to_the_true_open_set(
    both_shapes: ledger_rows.ParseResult,
) -> None:
    # RED on the pre-OMN-17573 parser: it skipped every line not starting with
    # a bar, so the only claim it saw was line 3 and it reported NONE open.
    open_lines = sorted(c.lineno for c in pairing.pair_rows(both_shapes.rows))
    assert open_lines == [9, 10, 16]


def test_timestamp_led_rows_carry_kind_lane_and_tickets(
    both_shapes: ledger_rows.ParseResult,
) -> None:
    by_line = {r.lineno: r for r in both_shapes.rows}
    assert (by_line[4].kind, by_line[4].lane, by_line[4].shape) == (
        "CLAIM",
        "token-lane",
        "ts",
    )
    assert by_line[4].tickets == frozenset({"OMN-101"})
    assert (by_line[9].kind, by_line[9].lane) == ("CLAIM", "open-lane")
    assert (by_line[10].kind, by_line[10].lane) == ("CLAIM", "barless-lane")
    assert by_line[15].kind == "TERMINAL"  # CLOSEOUT closes like TERMINAL
    assert by_line[17].kind == "REF"  # a STATUS row that closes a claim explicitly
    assert 11 not in by_line  # CLAIM-AMEND opens nothing
    assert 19 not in by_line  # a quoted '| CLAIM |' in a NOTE body is not a claim
    assert both_shapes.unparseable == []


def test_close_references_bind_the_claim_they_name(
    both_shapes: ledger_rows.ParseResult,
) -> None:
    bindings: dict[int, ledger_rows.Row] = {}
    pairing.pair_rows(both_shapes.rows, bindings)
    by_line = {r.lineno: r for r in both_shapes.rows}
    closer = {
        claim_line: bindings[id(by_line[claim_line])].lineno
        for claim_line in (3, 4, 5, 6, 7, 8)
    }
    # lane pair, token digest, ledger line, CLOSEOUT by lane, supersedes, lane=
    assert closer == {3: 12, 4: 13, 5: 14, 6: 15, 7: 16, 8: 17}


def test_line_citation_of_an_unrelated_claim_binds_nothing(
    both_shapes: ledger_rows.ParseResult,
) -> None:
    # Line 18 cites ledger:9, but shares neither lane nor ticket with it. A
    # roll renumbers lines, so an unrelated claim at a cited line is not proof.
    bindings: dict[int, ledger_rows.Row] = {}
    pairing.pair_rows(both_shapes.rows, bindings)
    claim_9 = next(r for r in both_shapes.rows if r.lineno == 9)
    assert id(claim_9) not in bindings


def test_shape_counts_and_positive_control_on_a_healthy_parse(
    both_shapes: ledger_rows.ParseResult,
) -> None:
    counts = both_shapes.shape_counts
    assert counts[("pipe", "CLAIM")] == 1
    assert counts[("ts", "CLAIM")] == 8
    assert counts[("ts", "TERMINAL")] == 4
    assert ledger_rows.positive_control(both_shapes) == []
    out = decision.render_report([], both_shapes, apply=False)
    assert "CLAIM: pipe-led 1, ts-led 8, heading 0" in out
    assert "positive control: 9 CLAIM rows parsed" in out


def test_kind_first_row_lane_is_the_next_cell_not_a_later_lane_field() -> None:
    row = ledger_rows.parse_pipe_row(
        "| 2026-09-21T22:50:57Z | CLAIM | dev-lane-stalled-recreate | actor=x "
        "| host=lab-host-a | lane=dev | repair the stalled recreate |",
        "t:20",
    )
    assert isinstance(row, ledger_rows.Row)
    assert row.lane == "dev-lane-stalled-recreate"


def test_positive_control_fails_loudly_when_no_claim_row_parses() -> None:
    # Bulleted CLAIM markers with no parsed rows must fail the positive control.
    ledger_text = "\n".join(
        [
            "- 2026-09-20T10:00:00Z | CLAIM | lane=unseen | ticket=OMN-1 | x",
            "- 2026-09-20T10:05:00Z | CLAIM | lane=unseen-2 | ticket=OMN-2 | y",
        ]
    )
    parsed = ledger_rows.load_rows((LEDGER_NAME, ledger_text), [])
    assert ledger_rows.positive_control(parsed) == [
        f"{LEDGER_NAME}: 2 line(s) hold '| CLAIM |' but zero CLAIM rows parsed"
    ]


PER_PR_FIXTURE = [
    "# Rolling work ledger",  # 1
    "",  # 2
    # ledger:4471 — the TERMINAL's ticket differs from the CLAIM's.
    "2026-09-22T04:32:04Z | CLAIM | lane=pr-fix-omniclaude-2286-body | ticket=OMN-17427 | repo=omniclaude | pr=2286 | scope=replace a stale citation",  # 3
    "2026-09-22T04:55:20Z | TERMINAL | lane=pr-fix-omniclaude-2286-body | ticket=OMN-18595 | repo=omniclaude | pr=2286 | outcome=MERGED",  # 4
    # ledger:4625 — the TERMINAL was appended after the CLAIM but typed an
    # earlier timestamp.
    "2026-09-22T05:54:00Z | CLAIM | lane=pr-fix-omnimemory-527-body-terra | ticket=OMN-18595 | repo=omnimemory | pr=527 | scope=replace a citation",  # 5
    "2026-09-22T05:53:04Z | TERMINAL | lane=pr-fix-omnimemory-527-body-terra | ticket=OMN-18595 | repo=omnimemory | pr=527 | outcome=PR body corrected",  # 6
    # ledger:5002 — ticket mismatch again.
    "2026-09-22T09:05:00Z | CLAIM | lane=pr-fix-omnibase_infra-3960 | ticket=OMN-17427 | repo=omnibase_infra | pr=3960 | resumes=4979",  # 7
    "2026-09-22T09:07:00Z | TERMINAL | lane=pr-fix-omnibase_infra-3960 | ticket=OMN-17292 | repo=omnibase_infra | pr=3960 | outcome=BLOCKED",  # 8
    # ledger:5121-5128 — one CLAIM per PR, TERMINALs per PR list; LIFO spent
    # the six TERMINALs on the six newest claims and left 3950 and 3953 open.
    *[
        f"2026-09-22T12:09:{i:02d}Z | CLAIM | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr={pr}"
        for i, pr in enumerate((3950, 3953, 3954, 3955, 3956, 3957, 3958, 3960))
    ],  # 9-16
    "2026-09-22T12:12:02Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3950 | outcome=MERGED",  # 17
    "2026-09-22T12:28:23Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3953,3954,3955,3956,3957,3958 | outcome=BLOCKED",  # 18
    "2026-09-22T12:32:40Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3960 | outcome=MERGED",  # 19
    "2026-09-22T13:51:01Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3953,3954,3955,3956 | outcome=MERGED",  # 20
    "2026-09-22T13:56:19Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3957 | outcome=MERGED",  # 21
    "2026-09-22T14:26:17Z | TERMINAL | lane=infra-land-1210 | ticket=OMN-17427 | repo=omnibase_infra | pr=3958 | outcome=CLOSED_SUPERSEDED",  # 22
    # ledger:5180-5184 — two per-PR claims, one TERMINAL naming both PRs in
    # prose with no pr= field; LIFO closed only the newer one.
    "2026-09-22T13:12:19Z | CLAIM | lane=market-bumps-1312 | ticket=OMN-17427 | repo=omnimarket | pr=2778",  # 23
    "2026-09-22T13:12:36Z | CLAIM | lane=market-bumps-1312 | ticket=OMN-17427 | repo=omnimarket | pr=2780",  # 24
    "2026-09-22T13:22:50Z | TERMINAL | lane=market-bumps-1312 | ticket=OMN-17427 | repo=omnimarket | #2778 CLOSED_SUPERSEDED. #2780 MERGED",  # 25
    # Negative control: a TERMINAL for another PR of the same lane closes
    # neither by PR nor by lane LIFO.
    "2026-09-22T15:00:00Z | CLAIM | lane=guard-lane | ticket=OMN-1 | repo=omnimarket | pr=1111",  # 26
    "2026-09-22T15:30:00Z | TERMINAL | lane=guard-lane | ticket=OMN-1 | repo=omnimarket | pr=2222 | outcome=MERGED",  # 27
]


def test_per_pr_claims_close_on_their_pr_within_the_lane() -> None:
    # RED on the registry_root#566 parser: lane LIFO left lines 3, 5, 7, 9, 10
    # and 23 open (live 4471, 4625, 5002, 5121, 5122, 5180), and closed line 26
    # with a TERMINAL for a different PR.
    parsed = ledger_rows.load_rows((LEDGER_NAME, "\n".join(PER_PR_FIXTURE)), [])
    assert parsed.shape_counts[("ts", "CLAIM")] == 14
    open_lines = sorted(c.lineno for c in pairing.pair_rows(parsed.rows))
    assert open_lines == [26]


def test_row_prs_reads_the_pr_field_then_terminal_prose() -> None:
    assert ledger_rows.row_prs(
        "CLAIM", "ticket=OMN-1 | pr=3950 | chain pr=3960/3950"
    ) == {"3950"}
    assert ledger_rows.row_prs("TERMINAL", "pr=3953,3954 | outcome=x") == {
        "3953",
        "3954",
    }
    assert ledger_rows.row_prs(
        "TERMINAL", "repo=omnimarket | #2778 CLOSED. #2780 MERGED"
    ) == {
        "2778",
        "2780",
    }
    # A CLAIM with no pr= field is about no PR in particular, whatever it cites.
    assert (
        ledger_rows.row_prs("CLAIM", "scope=fence check of omnimarket#2778")
        == frozenset()
    )


def test_recent_own_rows_reads_the_row_writer_not_lanes_named_in_bodies() -> None:
    text = "\n".join(
        [
            "2026-09-23T00:10:00Z | STATUS | lane=busy-lane | ticket=OMN-1 | still going",
            "2026-09-23T00:20:00Z | FRICTION | lane=reporter | source-lane=named-only | x",
            "| 2026-09-23T00:30:00Z | pipe-lane | OMN-2 | NOTE | a pipe-led own row |",
            "2026-09-22T20:00:00Z | STATUS | lane=stale-lane | too old",
        ]
    )
    active = findings.recent_own_rows(text, ts("2026-09-22T23:00:00Z"))
    assert set(active) == {"busy-lane", "reporter", "pipe-lane"}


DONE_FIXTURE = [
    "# Rolling work ledger",
    "",
    # ledger:678 and ledger:739, verbatim apart from truncation.
    "2026-09-18T14:54:26Z | CLAIM | lane=omniclaude-release-cut-after-18702-1500 | ticket=OMN-18652 | scope=omniclaude version bump + tag + release.yml main fast-forward after OMN-18702 merges; then rerun the parity job on omninode_infra#1556",  # 3
    "2026-09-18T16:24:08Z | DONE | lane=omniclaude-release-cut-after-18702-1500 | ticket=OMN-18652 | omniclaude v0.26.0 cut after OMN-18702 (omniclaude#2237, squash ccaf4b29a, merged 15:36:19Z)",  # 4
    # A DONE row of another lane and ticket closes nothing.
    "2026-09-18T15:00:00Z | CLAIM | lane=still-open | ticket=OMN-1 | scope=unrelated work",  # 5
    "2026-09-18T16:30:00Z | DONE | lane=someone-else | ticket=OMN-2 | finished other work",  # 6
]


def test_done_rows_close_like_terminal_rows() -> None:
    # RED before round 3: DONE was a non-claim class, so ledger:678 read as
    # dangling and the morning run closed it a second time on omninode_infra#1556.
    parsed = ledger_rows.load_rows((LEDGER_NAME, "\n".join(DONE_FIXTURE)), [])
    done = next(r for r in parsed.rows if r.lineno == 4)
    assert done.kind == "TERMINAL"
    assert [c.lineno for c in pairing.pair_rows(parsed.rows)] == [5]


DUPLICATE_FIXTURE = [
    "# Rolling work ledger",
    "",
    # split-20:787/788/801: the same CLAIM appended twice, 8 s apart, the
    # second with an estimate cell; one TERMINAL closed one of them.
    "2026-09-17T17:29:33Z | CLAIM | lane=contractor-eod-0917-verify-draft-1735 | Verifying the contractor's 2026-09-17T17:11Z EOD status claims (omnimarket#2619, OMN-18426 hook installs), drafting reply; no Slack/Linear sends",  # 3
    "2026-09-17T17:29:41Z | CLAIM | lane=contractor-eod-0917-verify-draft-1735 | est ~2 lane-hours; displaces nothing; (OMN-18613) | Verifying the contractor's 2026-09-17T17:11Z EOD status claims (omnimarket#2619, OMN-18426 hook installs), drafting reply; no Slack/Linear sends",  # 4
    "2026-09-17T17:41:00Z | TERMINAL | lane=contractor-eod-0917-verify-draft-1735 | closes-CLAIM=docs/tracking/ROLLING_WORK_LEDGER.md:3508 | OUTCOME: verified",  # 5
    # Negative controls: per-PR claims differ only in pr=; a claim re-appended
    # hours later is a restart, not a duplicate.
    "2026-09-17T18:00:00Z | CLAIM | lane=per-pr-lane | ticket=OMN-5 | repo=omnimarket | pr=11 | scope=land the cascade bump PR for the next infra release",  # 6
    "2026-09-17T18:00:05Z | CLAIM | lane=per-pr-lane | ticket=OMN-5 | repo=omnimarket | pr=12 | scope=land the cascade bump PR for the next infra release",  # 7
    "2026-09-17T18:20:00Z | TERMINAL | lane=per-pr-lane | ticket=OMN-5 | repo=omnimarket | pr=12 | outcome=MERGED",  # 8
    "2026-09-17T19:00:00Z | CLAIM | lane=restart-lane | ticket=OMN-6 | scope=rebuild the dev lane and read back its readiness probe",  # 9
    "2026-09-17T23:00:00Z | CLAIM | lane=restart-lane | ticket=OMN-6 | scope=rebuild the dev lane and read back its readiness probe",  # 10
    "2026-09-17T23:30:00Z | TERMINAL | lane=restart-lane | ticket=OMN-6 | outcome=done",  # 11
]


def test_a_duplicate_claim_closes_with_its_twin() -> None:
    # RED before round 3: LIFO spent line 5 on line 4 and left line 3 open,
    # and the morning run auto-closed it on omnimarket#2619.
    parsed = ledger_rows.load_rows((LEDGER_NAME, "\n".join(DUPLICATE_FIXTURE)), [])
    assert sorted(c.lineno for c in pairing.pair_rows(parsed.rows)) == [6, 9]


STALE_HOLD = (
    "2026-09-20T10:00:00Z | HOLD | lane=dead-holder | "
    "id=2026-09-20T10:00:00Z-dead-holder | to=all | ticket=OMN-17466 | "
    "pr=omnimarket#12 | after=omnimarket#12 | finish the PR"
)


def test_a_merged_pr_cannot_hide_an_unresolvable_cited_repo() -> None:
    ev = evidence.Evidence(
        prs=[
            evidence.PrHandle("market", "omnimarket", 12, state="MERGED"),
            evidence.PrHandle("unknown-repo", None, 13),
        ]
    )
    assert evidence.verdict_for(ev)[0] == evidence.UNKNOWN


def test_legacy_hold_formats_do_not_block_canonical_archive_reconciliation() -> None:
    archive = (
        "2026-07-15T16:17Z | HOLD | market#12 remains policy-held\n"
        "2026-07-16T16:11:00Z | actor | merge classification | HOLD | market#13\n"
    )
    parsed = ledger_rows.load_rows(
        (LEDGER_NAME, STALE_HOLD),
        [("ROLLING_WORK_LEDGER_legacy.md", archive)],
    )
    assert ledger_rows.positive_control(parsed) == []
    assert len(findings.open_holds(parsed.rows)) == 1


def test_built_rows_use_canonical_timestamp_led_shapes() -> None:
    terminal = findings.build_terminal_row(make_finding(evidence.COMPLETED), NOW)
    assert terminal.startswith(
        "2026-10-06T22:00:00Z | TERMINAL | lane=wave2-omn17350-check8 |"
    )
    assert "| actor=script:ledger-reconciler |" in terminal
    assert "| friction=none |" in terminal
    assert findings.AUTO_CLOSE_MARK in terminal
    attention = findings.build_attention_row(make_finding(evidence.ORPHANED), NOW)
    assert attention.startswith(
        "2026-10-06T22:00:00Z | STATUS | lane=ledger-reconciler |"
    )
