# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the owner summary node: facts, shape check and send watermark."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_owner_summary_effect.__main__ import main
from omnimarket.nodes.node_owner_summary_effect.handlers import HandlerOwnerSummary
from omnimarket.nodes.node_owner_summary_effect.handlers.handler_owner_summary import (
    CLASS_PLAIN,
    COVERAGE_SCOPE,
    DRAFT_RULES,
    PLAIN_WORDS,
    TICKET_COVERAGE_LIMIT,
)
from omnimarket.nodes.node_owner_summary_effect.models import (
    ModelCloneCommit,
    ModelMergedChange,
    ModelOwnerSummaryFacts,
    ModelOwnerSummaryRequest,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_draft_coverage import (
    ModelDraftCoverage,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_closed import (
    ModelLaneClosed,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_started import (
    ModelLaneStarted,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_m4_row import ModelM4Row

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_owner_summary_effect"
)

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
HANDLER = HandlerOwnerSummary()
GOOD = (
    "1 change merged across 1 repository. 0 work runs (lanes) closed.\n"
    "Merges are unconfirmed as running. Friction data is unavailable. omnimarket#12 landed OMN-100 and omniclaude#7 landed OMN-200.\n"
    "*M4 board (milestones)*\n- nothing moved as of the last lane report.\n"
    "(via Owner's agent)\n"
)


def _facts() -> ModelOwnerSummaryFacts:
    return ModelOwnerSummaryFacts(
        generated_at="2026-10-24T12:00:00Z",
        window_since="2026-10-24T00:00:00Z",
        window_until="2026-10-24T12:00:00Z",
        window_source="--since",
        merged_changes=(
            ModelMergedChange(
                repo="omnimarket",
                number=12,
                title="t",
                merged_at="2026-10-24T01:00:00Z",
                tickets=("OMN-100",),
            ),
        ),
        clone_only_commits=(
            ModelCloneCommit(
                repo="omniclaude", pr=7, tickets=("OMN-200",), subject="s"
            ),
        ),
    )


def test_good_draft_passes() -> None:
    assert HANDLER.check_draft(GOOD, _facts()) == []


def test_missing_footer_fails() -> None:
    problems = HANDLER.check_draft(
        GOOD.replace("(via Owner's agent)", "thanks"), _facts()
    )
    assert any("footer" in p for p in problems)


def test_fabricated_ticket_and_pr_fail() -> None:
    draft = GOOD.replace("OMN-100", "OMN-999").replace("omnimarket#12", "omnimarket#13")
    problems = HANDLER.check_draft(draft, _facts())
    assert any("OMN-999" in p for p in problems)
    assert any("omnimarket#13" in p for p in problems)


@pytest.mark.parametrize(
    ("known", "cited"),
    [("OMN-123", "OMN-12"), ("OMN-1234", "OMN-12"), ("OMN-12", "OMN-1234")],
)
def test_ticket_ids_require_whole_matches(known: str, cited: str) -> None:
    facts = _facts().model_copy(update={"errors": (known,)})
    assert (
        HANDLER.check_draft(GOOD + f"Gaps\n{known}\n(via Owner's agent)\n", facts) == []
    )
    problems = HANDLER.check_draft(
        GOOD + f"Gaps\n{cited}\n(via Owner's agent)\n", facts
    )
    assert problems == [f"{cited} is not in the facts"]


@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-10-23T12:00:00Z", "2026-10-23T00:00:00Z"),
        ("2026-10-23T12:00:00Z", "2026-10-23T12:00:00Z"),
        ("2026-10-24T12:00:00Z", ""),
        ("2026-10-24T13:00:00Z", ""),
    ],
)
def test_since_must_precede_window_end(tmp_path: Path, since: str, until: str) -> None:
    with pytest.raises(ValueError, match="--since must be before"):
        HANDLER.resolve_window(NOW, since, until, tmp_path / "state.json")
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=tmp_path,
            ledger=tmp_path / "ledger.md",
            since=since,
            until=until,
            now=NOW.isoformat(),
        )
    )
    assert result.exit_code == 2


@pytest.mark.parametrize("explicit", [False, True])
def test_reused_lane_keeps_new_claim_open(tmp_path: Path, explicit: bool) -> None:
    ledger = tmp_path / "ledger.md"
    reference = " | closes-CLAIM=2026-10-24T08:00:00Z" if explicit else ""
    ledger.write_text(
        "2026-10-24T08:00:00Z | CLAIM | lane=alpha | ticket=OMN-1\n"
        "2026-10-24T09:00:00Z | CLAIM | lane=beta | ticket=OMN-2\n"
        "2026-10-24T10:00:00Z | TERMINAL | lane=alpha | outcome=done"
        f"{reference}\n"
        "2026-10-24T11:00:00Z | CLAIM | lane=alpha | ticket=OMN-3\n"
        # A reference to another lane's claim cannot close that claim.
        "2026-10-24T11:30:00Z | TERMINAL | lane=gamma"
        " | closes-CLAIM=2026-10-24T09:00:00Z\n"
        + (
            "2026-10-24T11:45:00Z | TERMINAL | lane=alpha"
            " | closes-CLAIM=2026-10-24T08:00:00Z\n"
            if explicit
            else ""
        )
    )
    errors: list[str] = []
    lanes = HANDLER._lanes(ledger, NOW - timedelta(hours=12), NOW, errors)
    assert errors == []
    assert [(claim.lane, claim.ticket) for claim in lanes["started"]] == [
        ("beta", "OMN-2"),
        ("alpha", "OMN-3"),
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_terminal_after_window_does_not_close_claim(
    tmp_path: Path, explicit: bool
) -> None:
    ledger = tmp_path / "ledger.md"
    reference = " | closes-CLAIM=2026-10-24T08:00:00Z" if explicit else ""
    ledger.write_text(
        "2026-10-24T08:00:00Z | CLAIM | lane=alpha | ticket=OMN-1\n"
        "2026-10-24T13:00:00Z | TERMINAL | lane=alpha | outcome=done"
        f"{reference}\n"
    )
    errors: list[str] = []
    lanes = HANDLER._lanes(ledger, NOW - timedelta(hours=12), NOW, errors)
    assert errors == []
    assert lanes["count"] == 0
    assert [claim.ticket for claim in lanes["started"]] == ["OMN-1"]
    at_terminal = HANDLER._lanes(
        ledger, NOW - timedelta(hours=12), NOW + timedelta(hours=1), errors
    )
    assert at_terminal["started"] == ()


def test_secret_path_and_length_fail() -> None:
    home = "/".join(["", "Users", "someone", "x"])
    token = "xoxb" + "-1234567890-abcdef"
    draft = GOOD + f"token {token} {home} " + "word " * 500 + "\n(via Owner's agent)\n"
    text = "\n".join(HANDLER.check_draft(draft, _facts()))
    assert "token-shaped" in text
    assert "home or volume path" in text
    assert "words; at most" in text


def test_no_m4_line_and_unavailable_friction_fail() -> None:
    draft = GOOD.replace("M4", "board") + "*Friction*\n- x\n(via Owner's agent)\n"
    problems = HANDLER.check_draft(draft, _facts())
    assert any("M4" in p for p in problems)
    assert any("Friction section" in p for p in problems)
    prose = GOOD + "No friction data exists yet.\n(via Owner's agent)\n"
    assert HANDLER.check_draft(prose, _facts()) == []


def test_window_uses_last_sent_and_caps(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_sent_until": "2026-10-24T05:00:00Z"}))
    start, end, source = HANDLER.resolve_window(NOW, "", "", state)
    assert (start, end, source) == (
        datetime(2026, 10, 24, 5, tzinfo=UTC),
        NOW,
        "last-sent",
    )
    state.write_text(json.dumps({"last_sent_until": "2026-10-01T00:00:00Z"}))
    start, _, source = HANDLER.resolve_window(NOW, "", "", state)
    assert start == NOW - timedelta(hours=48)
    assert "capped" in source
    start, _, source = HANDLER.resolve_window(NOW, "", "", tmp_path / "absent.json")
    assert start == NOW - timedelta(hours=12)
    assert source.startswith("default")
    with pytest.raises(ValueError, match="not an ISO instant"):
        HANDLER.resolve_window(NOW, "not a time", "", state)


def _write_world(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    watcher = home / ".claude_scratch" / "pr-watcher"
    watcher.mkdir(parents=True)
    (watcher / "state.json").write_text(
        json.dumps(
            {
                "last_tick": "2026-10-24T11:59:00Z",
                "merges": {
                    "a#1": {
                        "repo": "a",
                        "number": 1,
                        "title": "fix(OMN-5): x (#1)",
                        "merged_at": "2026-10-24T10:00:00Z",
                        "author": "someone",
                    },
                    "a#2": {
                        "repo": "a",
                        "number": 2,
                        "title": "old",
                        "merged_at": "2026-10-20T10:00:00Z",
                        "author": "someone",
                    },
                },
                "prs": {
                    "a#3": {"cls": "red", "facts": {"draft": True}},
                    "b#4": {"cls": "red", "facts": {}},
                },
            }
        )
    )
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "\n".join(
            [
                "2026-10-20T10:00:00Z | TERMINAL | lane=old | outcome=done | out of window",
                "2026-10-24T08:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | note",
                "2026-10-24T08:30:00Z | CLAIM | lane=beta | ticket=OMN-2 | note",
                "2026-10-24T09:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=blocked"
                " | closes-CLAIM=2026-10-24T08:00:00Z | delegated=2 codex=abc | all done",
                "2026-10-24T09:10:00Z | TERMINAL | lane=landing-L123 | outcome=handed-off | ended",
                "2026-10-24T10:00:00Z | STATUS | lane=m4-c16-diag | ticket=OMN-3 | C16 red again",
            ]
        )
    )
    return home, ledger


def test_facts_merges_queue_lanes_and_no_authors(tmp_path: Path) -> None:
    home, ledger = _write_world(tmp_path)
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=home,
            ledger=ledger,
            since="2026-10-24T00:00:00Z",
            now="2026-10-24T12:00:00Z",
        )
    )
    assert result.exit_code == 0
    facts = result.facts
    assert facts is not None
    assert [(m.repo, m.number, m.tickets) for m in facts.merged_changes] == [
        ("a", 1, ("OMN-5",))
    ]
    assert "someone" not in facts.model_dump_json()
    assert (facts.open_total, facts.open_drafts) == (2, 1)
    assert facts.open_by_class == {"red": 2}
    assert [c.lane for c in facts.lanes_closed] == ["alpha"]
    assert facts.lanes_closed[0].codex is True
    assert facts.controller_worker_lanes_closed == 1
    assert [s.lane for s in facts.lanes_started_not_closed] == ["beta"]
    assert [r.lane for r in facts.m4_rows] == ["m4-c16-diag"]
    assert facts.friction_status == "unavailable"
    assert facts.errors == ()


def _facts_with_closed_lanes(
    tmp_path: Path, count: int, blocked: int
) -> ModelOwnerSummaryFacts:
    ledger = tmp_path / "ledger.md"
    start = NOW - timedelta(hours=12)
    ledger.write_text(
        "\n".join(
            f"{(start + timedelta(minutes=n)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f" | TERMINAL | lane=l{n:02d} | ticket=OMN-{1000 + n}"
            f" | outcome={'blocked' if n <= blocked else 'done'}"
            for n in range(1, count + 1)
        )
    )
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=tmp_path,
            ledger=ledger,
            since=start.isoformat(),
            now=NOW.isoformat(),
        )
    )
    assert result.exit_code == 0
    assert result.facts is not None
    return result.facts


def test_facts_list_every_blocked_lane_before_newer_ones(tmp_path: Path) -> None:
    facts = _facts_with_closed_lanes(tmp_path, count=90, blocked=5)
    assert facts.lanes_closed_count == 90
    assert len(facts.lanes_closed) == 80
    assert [lane.lane for lane in facts.lanes_closed if lane.outcome == "blocked"] == [
        f"l{n:02d}" for n in range(1, 6)
    ]
    assert [lane.lane for lane in facts.lanes_closed if lane.outcome == "done"] == [
        f"l{n:02d}" for n in range(16, 91)
    ]
    assert facts.lanes_closed_by_outcome == {"blocked": 5, "done": 85}
    assert facts.lanes_closed_omitted == 10
    timestamps = [lane.ts for lane in facts.lanes_closed]
    assert timestamps == sorted(timestamps)


def test_facts_lanes_over_the_limit_all_blocked(tmp_path: Path) -> None:
    facts = _facts_with_closed_lanes(tmp_path, count=100, blocked=100)
    assert facts.lanes_closed_count == 100
    assert [lane.lane for lane in facts.lanes_closed] == [
        f"l{n:02d}" for n in range(21, 101)
    ]
    assert facts.lanes_closed_by_outcome == {"blocked": 100}
    assert facts.lanes_closed_omitted == 20


def test_coverage_tickets_include_old_blocked_lanes(tmp_path: Path) -> None:
    facts = _facts_with_closed_lanes(tmp_path, count=90, blocked=5)
    assert set(facts.coverage.tickets) == {f"OMN-{1000 + n}" for n in range(1, 6)}


def test_facts_lanes_leave_out_handoffs_and_skips(tmp_path: Path) -> None:
    root, ledger = _write_world(tmp_path)
    outcomes = ("handed-off", "skipped", "skipped-owned", "done", "blocked", "")
    ledger.write_text(
        "\n".join(
            f"2026-10-24T09:{i:02d}:00Z | TERMINAL | lane=run-{i}"
            + (f" | outcome={outcome}" if outcome else "")
            for i, outcome in enumerate(outcomes)
        )
    )
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=root,
            ledger=ledger,
            since="2026-10-24T00:00:00Z",
            now=NOW.isoformat(),
        )
    )
    assert result.exit_code == 0
    facts = result.facts
    assert facts is not None
    assert [row.outcome for row in facts.lanes_closed] == ["done", "blocked"]
    assert facts.lanes_closed_count == 6
    assert facts.lanes_closed_omitted == 4
    assert facts.lanes_closed_by_outcome == {
        "handed-off": 1,
        "skipped": 1,
        "skipped-owned": 1,
        "done": 1,
        "blocked": 1,
        "unrecorded": 1,
    }


def test_facts_lanes_drop_the_runner_duplicate_row(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-10-24T09:00:00Z | TERMINAL | lane=alpha | outcome=done | substantive report\n"
        "2026-10-24T09:01:00Z | TERMINAL | lane=alpha | outcome=done | remote lane on h1 ended status=done\n"
        "2026-10-24T09:02:00Z | TERMINAL | lane=beta | outcome=done | remote lane on h1 ended status=done\n"
    )
    errors: list[str] = []
    lanes = HANDLER._lanes(ledger, NOW - timedelta(hours=12), NOW, errors)
    assert errors == []
    assert [(row.lane, row.text) for row in lanes["closed"]] == [
        ("alpha", "substantive report"),
        ("beta", "remote lane on h1 ended status=done"),
    ]
    assert lanes["count"] == 3
    assert lanes["outcomes"] == {"done": 3}
    assert lanes["omitted"] == 1


def test_facts_names_an_unreadable_source(tmp_path: Path) -> None:
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=tmp_path,
            ledger=tmp_path / "missing.md",
            since="2026-10-24T00:00:00Z",
            now="2026-10-24T12:00:00Z",
        )
    )
    assert result.facts is not None
    assert any("watcher state unreadable" in e for e in result.facts.errors)
    assert any("ledger unreadable" in e for e in result.facts.errors)


def test_friction_unavailable_then_ok(tmp_path: Path) -> None:
    errors: list[str] = []
    assert HANDLER._friction(None, errors)[0] == "unavailable"
    assert HANDLER._friction(tmp_path / "no.json", errors)[0] == "unavailable"
    data = tmp_path / "d.json"
    data.write_text(json.dumps({"top_dropped": [1, 2, 3, 4, 5, 6]}))
    status, _, top = HANDLER._friction(data, errors)
    assert status == "ok"
    assert top == ("1", "2", "3", "4", "5")
    data.write_text("{not json")
    assert HANDLER._friction(data, errors)[0] == "unavailable"
    assert errors


def test_cli_facts_check_and_mark_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home, ledger = _write_world(tmp_path)
    assert (
        main(
            [
                "facts",
                "--omni-home",
                str(home),
                "--ledger",
                str(ledger),
                "--since",
                "2026-10-24T00:00:00Z",
                "--now",
                "2026-10-24T12:00:00Z",
            ]
        )
        == 0
    )
    facts_text = capsys.readouterr().out
    assert json.loads(facts_text)["window_source"] == "--since"
    (tmp_path / "facts.json").write_text(facts_text)
    (tmp_path / "draft.md").write_text(
        "1 change merged across 1 repository. a#1 landed OMN-5.\n"
        "Merges are unconfirmed as running. Friction data is unavailable. 1 work runs (lanes) closed; "
        "1 controller worker run closed; 1 started and not closed; 2 open pull requests; 2 failing checks; 1 open pull request is a draft.\n"
        "OMN-1 blocked. M4 board (milestones): OMN-3 unchanged as of the last report.\n"
        "(via Owner's agent)\n"
    )
    checked = [
        "check",
        "--draft",
        str(tmp_path / "draft.md"),
        "--facts",
        str(tmp_path / "facts.json"),
    ]
    assert main(checked) == 0
    (tmp_path / "draft.md").write_text(
        "a#9 landed OMN-9. M4 unchanged.\n(via Owner's agent)\n"
    )
    assert main(checked) == 1
    assert "FAIL a#9" in capsys.readouterr().out
    assert (
        main(["mark-sent", "--omni-home", str(home), "--until", "2026-10-24T12:00:00Z"])
        == 0
    )
    state = json.loads(
        (home / ".onex_state" / "owner-summary" / "state.json").read_text()
    )
    assert state["last_sent_until"] == "2026-10-24T12:00:00Z"
    assert main(["mark-sent", "--omni-home", str(home), "--until", "garbage"]) == 2


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.owner-summary-command-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.owner-summary-command-completed.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]
    assert contract["terminal_event"] == bus["publish_topics"][0]
    assert contract["node_type"] == "effect"


def _coverage_facts() -> ModelOwnerSummaryFacts:
    merges = (
        (
            "omnibase_core",
            1028,
            "fix(OMN-17427): the landing-metrics tick passes its ledger to the scorecard step",
            ("OMN-17427",),
        ),
        (
            "omniclaude",
            2962,
            "fix(OMN-21348): on a pull request another branch's hosted placement warns, not fails",
            ("OMN-21348",),
        ),
        *(
            (
                "omniintelligence",
                number,
                f"chore(deps): bump package-{number} from x to y",
                (),
            )
            for number in (1141, 1142, 1143)
        ),
    )
    return _facts().model_copy(
        update={
            "merged_changes": tuple(
                ModelMergedChange(
                    repo=repo,
                    number=number,
                    title=title,
                    tickets=tickets,
                    merged_at=NOW.isoformat(),
                )
                for repo, number, title, tickets in merges
            ),
            "clone_only_commits": (),
            "m4_rows": (
                ModelM4Row(
                    ts=NOW.isoformat(), row_type="STATUS", lane="m4", ticket="OMN-17292"
                ),
            ),
            "lanes_closed": tuple(
                ModelLaneClosed(
                    ts=NOW.isoformat(), lane=f"run-{i}", ticket=ticket, outcome=outcome
                )
                for i, (ticket, outcome) in enumerate(
                    (
                        ("OMN-20174", "blocked"),
                        ("OMN-20074", "blocked"),
                        ("OMN-21099", "partial"),
                        ("OMN-19998", "done"),
                        ("OMN-21350", "handed-off"),
                        ("OMN-16228", "skipped"),
                    )
                )
            ),
            "lanes_closed_count": 6,
            "controller_worker_lanes_closed": 93,
            "lanes_started_not_closed": tuple(
                ModelLaneStarted(ts=NOW.isoformat(), lane=f"open-{i}") for i in range(2)
            ),
            "open_total": 488,
        }
    )


def _full_draft() -> str:
    return (
        "5 changes merged across 3 repositories\n"
        "(omnibase_core#1028 OMN-17427) passes the shared work log to the report card step.\n"
        "(omniclaude#2962 OMN-21348) warns when another branch's hosted job runs elsewhere.\n"
        "3 dependency bumps merged.\n"
        "6 work runs (lanes) closed; 93 controller worker runs closed; "
        "2 started and not closed; 488 open pull requests.\n"
        "M4 board (milestones): OMN-17292. Blocked: OMN-20174, OMN-20074. Partial: OMN-21099.\n"
        "The merges are unconfirmed as running.\n"
        "Friction data is unavailable.\n"
        "(via Owner's agent)\n"
    )


def test_coverage_full_draft_passes() -> None:
    assert HANDLER.check_draft(_full_draft(), _coverage_facts()) == []


@pytest.mark.parametrize("separator", ["(", ")", ";", "\n"])
def test_coverage_ticket_must_sit_beside_the_change(separator: str) -> None:
    facts = _coverage_facts()
    draft = _full_draft().replace(
        "omnibase_core#1028 OMN-17427",
        f"omnibase_core#1028{separator}OMN-17427",
    )
    assert HANDLER.check_draft(draft, facts) == [
        "merged changes cited without their ticket in the same group: omnibase_core#1028 (OMN-17427)"
    ]
    assert HANDLER.check_draft(_full_draft(), facts) == []
    repeated = _full_draft().replace(
        "The merges", "omnibase_core#1028; omnibase_core#1028; The merges"
    )
    assert HANDLER.check_draft(repeated, facts) == [
        "merged changes cited without their ticket in the same group: omnibase_core#1028 (OMN-17427)"
    ]
    both = draft.replace("omniclaude#2962 OMN-21348", "omniclaude#2962; OMN-21348")
    assert HANDLER.check_draft(both, facts) == [
        "merged changes cited without their ticket in the same group: omnibase_core#1028 (OMN-17427), omniclaude#2962 (OMN-21348)"
    ]
    wrong_ticket = _full_draft().replace(
        "omnibase_core#1028 OMN-17427",
        "omnibase_core#1028 OMN-17427x; OMN-17427",
    )
    assert HANDLER.check_draft(wrong_ticket, facts) == [
        "merged changes cited without their ticket in the same group: omnibase_core#1028 (OMN-17427)"
    ]
    absent = _full_draft().replace("omnibase_core#1028", "the change")
    assert any(
        "uncited rows are not named" in p for p in HANDLER.check_draft(absent, facts)
    )


def test_coverage_friction_gap_is_named() -> None:
    facts = _coverage_facts()
    draft = _full_draft().replace("Friction data is unavailable.\n", "")
    assert HANDLER.check_draft(draft, facts) == [
        "the friction data is unavailable; say so in one clause (not as a heading)"
    ]
    assert HANDLER.check_draft(_full_draft(), facts) == []
    assert (
        HANDLER.check_draft(_full_draft().replace("Friction", "FRICTION"), facts) == []
    )
    assert (
        HANDLER.check_draft(draft, facts.model_copy(update={"friction_status": "ok"}))
        == []
    )


def test_coverage_superlatives_are_refused() -> None:
    draft = _full_draft().replace(
        "The merges", "First, only, fully, entirely, entire. FIRST. The merges"
    )
    assert HANDLER.check_draft(draft, _coverage_facts()) == [
        f'"{word}" asserts what no fact row says; drop it or cite the row'
        for word in ("first", "only", "fully", "entirely", "entire")
    ]


def test_coverage_superlatives_ignore_hyphenated_terms() -> None:
    draft = _full_draft().replace(
        "The merges", "A JSON-only retry and a first-party check. The merges"
    )
    assert HANDLER.check_draft(draft, _coverage_facts()) == []


def test_coverage_headline_must_match_the_facts() -> None:
    headline = "5 changes merged across 3 repositories"
    for replacement in ("10 changes merged across 4 repositories", ""):
        assert HANDLER.check_draft(
            _full_draft().replace(headline, replacement), _coverage_facts()
        ) == [f'the opening must say "{headline}"']


def test_coverage_every_merged_ticket_is_cited() -> None:
    problems = HANDLER.check_draft(
        _full_draft().replace("OMN-17427", ""), _coverage_facts()
    )
    assert any("uncited rows are not named" in p for p in problems)
    assert (
        "merged changes cited without their ticket in the same group: omnibase_core#1028 (OMN-17427)"
        in problems
    )
    facts = _coverage_facts()
    untracked = facts.merged_changes[0].model_copy(update={"tickets": ()})
    facts = facts.model_copy(
        update={"merged_changes": (untracked, *facts.merged_changes[1:])}
    )
    assert HANDLER.coverage_of(facts).untracked_changes == ("omnibase_core#1028",)
    for replacement in (
        "the change",
        "omnibase_core#10280",
        "omnibase_core#1028x",
    ):
        problems = HANDLER.check_draft(
            _full_draft().replace("omnibase_core#1028", replacement), facts
        )
        assert any("uncited rows are not named" in p for p in problems)
        assert not any("with no ticket not cited" in problem for problem in problems)
    assert HANDLER.coverage_of(_coverage_facts()).merged_tickets == (
        "OMN-17427",
        "OMN-21348",
    )


def test_coverage_dependency_bumps_are_said_once() -> None:
    assert HANDLER.check_draft(
        _full_draft().replace("3 dependency bumps merged.", ""), _coverage_facts()
    ) == ["3 dependency bumps merged; say so in one clause"]


def test_coverage_m4_ticket_is_cited() -> None:
    assert HANDLER.check_draft(
        _full_draft().replace("OMN-17292", ""), _coverage_facts()
    ) == ["tickets not cited (M4 rows, blocked or partial lanes): OMN-17292"]


def test_coverage_blocked_and_partial_tickets_are_cited() -> None:
    facts = _coverage_facts()
    assert HANDLER.check_draft(
        _full_draft().replace("OMN-20174", "").replace("OMN-21099", ""), facts
    ) == ["tickets not cited (M4 rows, blocked or partial lanes): OMN-20174, OMN-21099"]
    assert not {"OMN-19998", "OMN-21350", "OMN-16228"}.intersection(
        HANDLER.coverage_of(facts).tickets
    )


def test_coverage_blocked_and_partial_kinds_must_be_named() -> None:
    facts = _coverage_facts()
    facts = facts.model_copy(
        update={
            "lanes_closed": (
                *facts.lanes_closed,
                facts.lanes_closed[0].model_copy(update={"ticket": "OMN-21099"}),
            )
        }
    )
    draft = _full_draft().replace(
        "M4 board (milestones): OMN-17292. Blocked: OMN-20174, OMN-20074. Partial: OMN-21099.",
        "M4 board (milestones): OMN-17292.\n"
        "Blocked: OMN-20174, OMN-20074.\nBlocked: OMN-21099.",
    )
    problem = (
        "tickets whose blocked or partial work is not named as such: "
        "OMN-21099 (partial)"
    )
    assert HANDLER.check_draft(draft, facts) == [problem]
    for wording in ("BLOCKED and PARTIAL", "blocked\nOMN-21099 partial"):
        assert (
            HANDLER.check_draft(
                draft.replace("Blocked: OMN-21099", f"OMN-21099 {wording}"), facts
            )
            == []
        )
    for wording in (
        "blocked partially",
        "blocked\npartial",
        "blocked\nOMN-21099x partial",
    ):
        assert HANDLER.check_draft(
            draft.replace("Blocked: OMN-21099", f"OMN-21099 {wording}"), facts
        ) == [problem]
    assert HANDLER.check_draft(
        draft.replace("Blocked: OMN-21099", "Partial: OMN-21099"), facts
    ) == [problem.replace("(partial)", "(blocked)")]
    assert HANDLER.check_draft(
        draft.replace("Blocked: OMN-21099", "OMN-21099"), facts
    ) == [problem.replace("(partial)", "(blocked, partial)")]
    assert HANDLER.check_draft(
        draft.replace("Blocked: OMN-21099", "OMN-21099").replace("Blocked:", "Status:"),
        facts,
    ) == [
        "tickets whose blocked or partial work is not named as such: "
        "OMN-20174 (blocked), OMN-20074 (blocked), OMN-21099 (blocked, partial)"
    ]
    # The merged tickets on their own lines need no kind word.
    assert HANDLER.check_draft(_full_draft(), _coverage_facts()) == []


@pytest.mark.parametrize("term", ["receipt", "receipts", "RECEIPTS"])
def test_coverage_receipts_are_shorthand(term: str) -> None:
    draft = _full_draft().replace("The merges", f"{term}. The merges")
    assert HANDLER.check_draft(draft, _coverage_facts()) == [
        f'"{term.lower()}" is shorthand; write "recorded evidence"'
    ]
    assert PLAIN_WORDS["receipt"] == "recorded evidence"
    assert (
        HANDLER.check_draft(draft.replace(term, "recorded evidence"), _coverage_facts())
        == []
    )


def test_coverage_scope_states_what_the_check_requires() -> None:
    for field in ("tickets", "counts", "changes"):
        assert field in COVERAGE_SCOPE
    assert "420" in COVERAGE_SCOPE


def test_coverage_all_merged_changes_are_cited_or_counted() -> None:
    facts = _facts().model_copy(
        update={
            "merged_changes": tuple(
                ModelMergedChange(
                    repo="omnimarket",
                    number=i + 1,
                    title="feature",
                    tickets=("OMN-100",),
                    merged_at=(NOW - timedelta(minutes=30 - i)).isoformat(),
                )
                for i in range(30)
            ),
            "clone_only_commits": (),
        }
    )
    coverage = HANDLER.coverage_of(facts)
    assert coverage.changes == tuple(f"omnimarket#{i + 1}" for i in range(30))
    draft = (
        "30 changes merged across 1 repository. "
        + "; ".join(f"{change} OMN-100" for change in coverage.changes)
        + ". M4 board (milestones). Merges are unconfirmed as running. "
        "Friction data is unavailable.\n(via Owner's agent)\n"
    )
    assert HANDLER.check_draft(draft, facts) == []
    missing = draft.replace("omnimarket#24 OMN-100", "OMN-100")
    assert any("not named" in p for p in HANDLER.check_draft(missing, facts))
    named = missing.replace(
        "(via",
        f"Not listed: 1 merged change (1 other changes). Full list: {coverage.full_list_link}\n(via",
    )
    assert HANDLER.check_draft(named, facts) == []
    # Untracked changes use the same omission accounting.
    untracked = facts.merged_changes[-1].model_copy(update={"tickets": ()})
    beyond = facts.model_copy(
        update={"merged_changes": (*facts.merged_changes[:-1], untracked)}
    )
    assert HANDLER.check_draft(named, beyond) == []


def test_coverage_feeds_earlier_findings_forward(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rule = "When a review answers REVISE, write the next review prompt with the earlier answers under the heading === EARLIER FINDINGS === and, after each finding, the line applied or not applied: <the fact that contradicts it>; the rubric and the facts do not change between rounds."
    assert DRAFT_RULES[-1] == rule
    root, ledger = _write_world(tmp_path)
    assert (
        main(
            [
                "facts",
                "--omni-home",
                str(root),
                "--ledger",
                str(ledger),
                "--since",
                "2026-10-24T00:00:00Z",
                "--now",
                NOW.isoformat(),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "=== EARLIER FINDINGS ===" in output
    assert json.loads(output)["draft_rules"][-1] == rule


def test_coverage_counts_are_stated() -> None:
    assert HANDLER.check_draft(_full_draft().replace("93", ""), _coverage_facts()) == [
        "numbers not stated: controller worker lanes closed 93"
    ]


def test_coverage_counts_state_every_outcome() -> None:
    outcomes = {
        "blocked": 28,
        "partial": 9,
        "handed-off": 40,
        "done": 15,
        "skipped": 8,
        "skipped-owned": 7,
        "unrecorded": 1,
        "unused": 0,
    }
    facts = _coverage_facts().model_copy(
        update={"lanes_closed_count": 108, "lanes_closed_by_outcome": outcomes}
    )
    complete = (
        _full_draft()
        .replace("6 work runs", "108 work runs")
        .replace(
            "(via",
            "28 blocked; 9 partial; 40 handed off; 15 done; 8 skipped; 7 skipped-owned; 1 unrecorded.\n"
            "Not listed: 105 closed work runs (26 as blocked, 8 as partial, 40 as handed-off, "
            "15 as done, 8 as skipped, 7 as skipped-owned, 1 as unrecorded). Full list: "
            f"{HANDLER.coverage_of(facts).full_list_link}\n(via",
        )
    )
    assert HANDLER.check_draft(complete, facts) == []
    assert HANDLER.check_draft(complete.replace("9 partial;", "partial;"), facts) == [
        "numbers not stated: lanes closed as partial 9"
    ]
    coverage = HANDLER.coverage_of(facts)
    assert list(coverage.counts)[-7:] == [
        f"lanes closed as {outcome}" for outcome in outcomes if outcome != "unused"
    ]
    all_done = facts.model_copy(update={"lanes_closed_by_outcome": {"done": 108}})
    assert HANDLER.coverage_of(all_done).counts["lanes closed as done"] == 108


def test_coverage_ticket_kinds_say_partial_or_blocked() -> None:
    facts = _coverage_facts()
    coverage = HANDLER.coverage_of(facts)
    assert coverage.ticket_kinds == {
        "OMN-17292": "m4",
        "OMN-17427": "merged",
        "OMN-21348": "merged",
        "OMN-20174": "blocked",
        "OMN-20074": "blocked",
        "OMN-21099": "partial",
    }
    shared = facts.model_copy(
        update={
            "lanes_closed": (
                *facts.lanes_closed,
                facts.lanes_closed[0].model_copy(update={"ticket": "OMN-21099"}),
                facts.lanes_closed[0].model_copy(update={"ticket": "OMN-17427"}),
            ),
            "m4_rows": (
                *facts.m4_rows,
                facts.m4_rows[0].model_copy(update={"ticket": "OMN-17427"}),
            ),
        }
    )
    kinds = HANDLER.coverage_of(shared).ticket_kinds
    assert kinds["OMN-21099"] == "blocked,partial"
    assert kinds["OMN-17427"] == "blocked,m4,merged"
    assert set(kinds) == set(HANDLER.coverage_of(shared).tickets)
    assert DRAFT_RULES[1].endswith(
        " Name a ticket's kind as ticket_kinds gives it (a partial ticket is not blocked)."
    )


@pytest.mark.parametrize(
    "term",
    [
        "green",
        "ledger",
        "landing controller",
        "routing receipt",
        "steering prompts",
        "placement",
        "red",
        "installer contract",
        "launch unit",
        "pinned",
        "quorum",
        "heartbeat",
        "compaction",
        "claims table",
        "scorecard",
    ],
)
def test_coverage_plain_language_refuses_shorthand(term: str) -> None:
    addition = "green but not armed" if term == "green" else term
    matched = (
        ("green", "armed")
        if term == "green"
        else ("routing receipt", "receipt")
        if term == "routing receipt"
        else (term,)
    )
    assert HANDLER.check_draft(_full_draft(), _coverage_facts()) == []
    assert HANDLER.check_draft(
        _full_draft().replace("The merges", f"{addition}. The merges"),
        _coverage_facts(),
    ) == [
        f'"{word}" is shorthand; write "{PLAIN_WORDS[word.rstrip("s") if word == "steering prompts" else word]}"'
        for word in matched
    ]


def test_coverage_lanes_and_m4_are_glossed() -> None:
    draft = _full_draft().replace("work runs", "runs").replace("milestones", "board")
    assert HANDLER.check_draft(draft, _coverage_facts()) == [
        'define lanes once: write "work runs (lanes)"',
        'define M4 once: write "M4 board (milestones)"',
    ]


def test_coverage_overclaim_and_unconfirmed() -> None:
    for phrase, match in (
        ("can now be scheduled", "can now"),
        ("now always reads", "now always"),
    ):
        assert HANDLER.check_draft(
            _full_draft().replace("The merges", f"It {phrase}. The merges"),
            _coverage_facts(),
        ) == [
            f'"{match}" claims a running service; a merge is a change, say what is unconfirmed'
        ]
    assert HANDLER.check_draft(
        _full_draft().replace("The merges are unconfirmed as running.", ""),
        _coverage_facts(),
    ) == [
        "the draft never says the merges are unconfirmed as running; a merge is not a deploy"
    ]


def test_coverage_gaps_section_is_required_for_errors() -> None:
    facts = _coverage_facts().model_copy(
        update={"errors": ("pr watcher state is 3000 s old",)}
    )
    assert HANDLER.check_draft(_full_draft(), facts) == [
        "the facts hold errors; add a Gaps section that names each one"
    ]
    assert (
        HANDLER.check_draft(
            _full_draft().replace(
                "(via",
                "Gaps\nThe pull request watcher state is 3000 seconds old.\n(via",
            ),
            facts,
        )
        == []
    )


def test_coverage_ticket_list_is_bounded() -> None:
    facts = _coverage_facts().model_copy(
        update={
            "lanes_closed": tuple(
                ModelLaneClosed(
                    ts=NOW.isoformat(),
                    lane=f"blocked-{i}",
                    ticket=f"OMN-{30000 + i}",
                    outcome="blocked",
                )
                for i in range(30)
            ),
            "lanes_closed_count": 30,
        }
    )
    assert HANDLER.coverage_of(facts).tickets == (
        "OMN-17292",
        "OMN-17427",
        "OMN-21348",
        *(f"OMN-{30000 + i}" for i in range(TICKET_COVERAGE_LIMIT - 3)),
    )
    assert len(HANDLER.coverage_of(facts).tickets) == TICKET_COVERAGE_LIMIT


def test_coverage_is_derived_from_the_facts_not_the_stored_copy() -> None:
    facts = _coverage_facts()
    old = facts.model_dump(
        exclude={"coverage", "open_by_class_plain", "plain_words", "draft_rules"}
    )
    restored = ModelOwnerSummaryFacts.model_validate_json(json.dumps(old))
    assert HANDLER.coverage_of(restored) == HANDLER.coverage_of(facts)
    assert HANDLER.coverage_of(restored).tickets == (
        "OMN-17292",
        "OMN-17427",
        "OMN-21348",
        "OMN-20174",
        "OMN-20074",
        "OMN-21099",
    )
    empty = facts.model_copy(update={"coverage": ModelDraftCoverage()})
    assert HANDLER.check_draft(_full_draft().replace("OMN-17292", ""), empty) == [
        "tickets not cited (M4 rows, blocked or partial lanes): OMN-17292"
    ]


def test_coverage_facts_command_hands_the_drafter_its_lists(tmp_path: Path) -> None:
    root, ledger = _write_world(tmp_path)
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=root,
            ledger=ledger,
            since="2026-10-24T00:00:00Z",
            now=NOW.isoformat(),
        )
    )
    assert result.exit_code == 0
    facts = result.facts
    assert facts is not None
    assert facts.coverage.headline == "1 change merged across 1 repository"
    assert facts.coverage.merged_tickets == ("OMN-5",)
    assert facts.coverage.untracked_changes == ()
    assert facts.coverage.tickets == ("OMN-3", "OMN-5", "OMN-1")
    assert facts.coverage.counts == {
        "lanes closed": 1,
        "controller worker lanes closed": 1,
        "lanes started and not closed": 1,
        "open pull requests": 2,
        "failing checks": 2,
        "open pull requests that are drafts": 1,
        "lanes closed as blocked": 1,
    }
    assert facts.open_by_class_plain == {"red": "failing checks"}
    assert "ledger" in facts.plain_words
    assert facts.draft_rules == DRAFT_RULES
    assert len(DRAFT_RULES) == 10
    assert DRAFT_RULES[8] == (
        "Cite a merged change inside the same parentheses or semicolon group as its ticket; call blocked work blocked and partial work partial on the line that cites its ticket; name the friction gap in one clause; do not write first, only, fully or entire."
    )
    assert "420 words" in DRAFT_RULES[0]
    assert "Not listed:" in DRAFT_RULES[2]


def test_coverage_scope_describes_the_check(tmp_path: Path) -> None:
    root, ledger = _write_world(tmp_path)
    result = HANDLER.handle(
        ModelOwnerSummaryRequest(
            command="facts",
            root=root,
            ledger=ledger,
            since="2026-10-24T00:00:00Z",
            now=NOW.isoformat(),
        )
    )
    assert result.exit_code == 0
    assert result.facts is not None
    assert result.facts.coverage.scope == COVERAGE_SCOPE
    assert "tickets" in COVERAGE_SCOPE
    assert "counts" in COVERAGE_SCOPE
    old = result.facts.model_dump(exclude={"coverage"})
    restored = ModelOwnerSummaryFacts.model_validate_json(json.dumps(old))
    assert restored.coverage.scope == ""
    assert HANDLER.coverage_of(restored).scope == COVERAGE_SCOPE


def test_coverage_dry_run_round_trip_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, ledger = _write_world(tmp_path)
    assert (
        main(
            [
                "facts",
                "--omni-home",
                str(root),
                "--ledger",
                str(ledger),
                "--since",
                "2026-10-24T00:00:00Z",
                "--now",
                NOW.isoformat(),
            ]
        )
        == 0
    )
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(capsys.readouterr().out)
    draft_file = tmp_path / "draft.md"
    draft = (
        "1 change merged across 1 repository. a#1 changed OMN-5.\n"
        "1 work runs (lanes) closed; 1 controller worker run closed; "
        "1 started and not closed; 2 open pull requests; 2 failing checks; 1 open pull request is a draft.\n"
        "M4 board (milestones): OMN-3. Blocked: OMN-1.\n"
        "Merges are unconfirmed as running. Friction data is unavailable.\n(via Owner's agent)\n"
    )
    checked = ["check", "--draft", str(draft_file), "--facts", str(facts_file)]
    draft_file.write_text(draft.replace("OMN-3", ""))
    assert main(checked) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "OMN-3" in output
    draft_file.write_text(draft)
    assert main(checked) == 0
    assert "PASS" in capsys.readouterr().out


def test_coverage_every_reviewer_class_has_a_rule() -> None:
    facts = _coverage_facts()
    drafts = {
        "headline/merge omitted": _full_draft().replace("OMN-17427", ""),
        "counts": _full_draft().replace("93", ""),
        "M4 ticket": _full_draft().replace("OMN-17292", ""),
        "blocked/partial ticket": _full_draft().replace("OMN-21099", ""),
        "shorthand": _full_draft().replace("The merges", "green. The merges"),
        "overclaim": _full_draft().replace("The merges", "It can now run. The merges"),
        "gaps": _full_draft(),
    }
    for category, draft in drafts.items():
        case = (
            facts.model_copy(update={"errors": ("source unavailable",)})
            if category == "gaps"
            else facts
        )
        assert HANDLER.check_draft(draft, case), category
    assert HANDLER.check_draft(_full_draft(), facts) == []


def test_coverage_scales_to_a_twelve_hour_window() -> None:
    tickets = tuple(f"OMN-{40000 + i}" for i in range(12))
    facts = _facts().model_copy(
        update={
            "merged_changes": tuple(
                ModelMergedChange(
                    repo="omnimarket",
                    number=i + 1,
                    title="feature" if i < 60 else "chore(deps): update package",
                    tickets=(tickets[i % 12],) if i < 60 else (),
                    merged_at=(
                        NOW - timedelta(hours=12) + timedelta(minutes=10 * (i + 1))
                    ).isoformat(),
                )
                for i in range(63)
            ),
            "clone_only_commits": (),
        }
    )
    coverage = HANDLER.coverage_of(facts)
    assert coverage.merged_tickets == tickets
    assert coverage.untracked_changes == ()
    assert coverage.dependency_changes == 3
    draft = (
        "63 changes merged across 1 repository. 3 dependency bumps. "
        + "; ".join(f"omnimarket#{i + 1} {tickets[i % 12]}" for i in range(2))
        + " M4 board (milestones). Merges are unconfirmed as running. Friction data is unavailable. "
    )
    footer = "(via Owner's agent)"
    draft += " ".join(["update"] * (400 - len(draft.split()) - len(footer.split())))
    omitted = f"Not listed: 58 merged changes (58 other changes), 10 tickets. Full list: {coverage.full_list_link}"
    draft += "\n" + omitted + "\n" + footer
    assert len(draft.split()) <= 420
    assert HANDLER.check_draft(draft, facts) == []


def test_coverage_queue_classes_are_counted() -> None:
    classes = {
        "green-unarmed": 71,
        "red": 52,
        "pending": 33,
        "custom": 24,
        "queued": 24,
        "unread": 10,
    }
    facts = _coverage_facts().model_copy(
        update={"open_by_class": classes, "open_drafts": 8}
    )
    assert HANDLER.coverage_of(facts).counts == {
        "lanes closed": 6,
        "controller worker lanes closed": 93,
        "lanes started and not closed": 2,
        "open pull requests": 488,
        CLASS_PLAIN["green-unarmed"]: 71,
        CLASS_PLAIN["red"]: 52,
        CLASS_PLAIN["pending"]: 33,
        "custom": 24,
        "open pull requests that are drafts": 8,
    }
    draft = _full_draft().replace(
        "(via", "52 failing checks; 33 checks still running; 24 custom; 8 drafts.\n(via"
    )
    assert HANDLER.check_draft(draft, facts) == [
        "numbers not stated: passing checks, not set to merge automatically 71"
    ]
    assert (
        HANDLER.check_draft(
            draft.replace("52 failing", "71 passing checks; 52 failing"), facts
        )
        == []
    )
