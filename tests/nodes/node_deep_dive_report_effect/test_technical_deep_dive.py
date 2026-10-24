# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded-day, refusal and CLI coverage for the nightly technical report."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.nodes.node_deep_dive_report_effect import __main__ as cli
from omnimarket.nodes.node_deep_dive_report_effect.deep_dive.technical_report import (
    UncitedClaimError,
    build_claims,
    check_citations,
    parse_ledger_row,
    render_technical_report,
    rows_for_day,
    scrub,
)
from omnimarket.nodes.node_deep_dive_report_effect.handlers import (
    HandlerTechnicalDeepDive,
    handler_technical_deep_dive,
)
from omnimarket.nodes.node_deep_dive_report_effect.models import (
    EnumReportSection,
    ModelClaimCitation,
    ModelLedgerRef,
    ModelReportClaim,
    ModelTechnicalDeepDiveCommand,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parent / "fixtures"
_DAY = "2026-10-23"
_HEADINGS = (
    "What changed, by area",
    "Causes found and their fixes",
    "Decisions made and restated",
    "Open blockers",
    "Do not rediscover",
)


@pytest.fixture
def recorded_command() -> ModelTechnicalDeepDiveCommand:
    """Read the recorded day's three data sources at the test boundary."""
    return ModelTechnicalDeepDiveCommand(
        correlation_id=uuid4(),
        report_date=_DAY,
        owner_summary_facts=json.loads(
            (_FIXTURES / "recorded_day_facts.json").read_text(encoding="utf-8")
        ),
        dropped_decisions=json.loads(
            (_FIXTURES / "recorded_day_dropped_decisions.json").read_text(
                encoding="utf-8"
            )
        ),
        ledger_rows=tuple(
            split_ledger_rows(
                (_FIXTURES / "recorded_day_ledger.md").read_text(encoding="utf-8")
            )
        ),
    )


def _section(markdown: str, heading: str) -> str:
    return markdown.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


def _uncited_claim() -> ModelReportClaim:
    return ModelReportClaim(
        section=EnumReportSection.DECISIONS,
        area="general",
        text="Planted unsupported statement",
        citation=ModelClaimCitation(),
    )


def test_technical_deep_dive_renders_every_section_with_citations(
    recorded_command: ModelTechnicalDeepDiveCommand,
) -> None:
    result = HandlerTechnicalDeepDive().handle(recorded_command)
    report = result.report_markdown
    for heading in _HEADINGS:
        assert f"## {heading}\n" in report
        bullets = [
            line
            for line in _section(report, heading).splitlines()
            if line.startswith("- ")
        ]
        assert bullets
        for line in bullets:
            if line == f"- None recorded for {_DAY}.":
                continue
            assert " — " in line
            citation = line.rsplit(" — ", 1)[1]
            assert re.search(r"#\d+|[0-9a-f]{7,40}|ledger `", citation)

    changes = _section(report, _HEADINGS[0])
    assert changes.index("### omnibase_core") < changes.index("### omnimarket")
    assert "omnimarket#4332" in _section(report, _HEADINGS[1])
    assert "closing row: A missing terminal field" in _section(report, _HEADINGS[1])
    assert "lane-blocked" in _section(report, _HEADINGS[3])
    assert "Release gate waits" in _section(report, _HEADINGS[3])
    do_not_rediscover = _section(report, _HEADINGS[4])
    assert "Retrying an absent field" in do_not_rediscover
    assert "lane-superseded" in do_not_rediscover
    assert "existing citation validator" in do_not_rediscover
    decisions = _section(report, _HEADINGS[2])
    assert "Carry citations through every report section." in decisions
    assert "Restated: Retain the original evidence" in decisions
    assert "2026-10-22" not in report
    assert "Unlinked local experiment" not in report
    assert "Untraceable decision" not in report
    assert "Steering is not" not in report
    assert "Timeline is not" not in report
    assert "starting work on 2026-10-24" in report
    assert "- source error: Optional source unavailable" in report
    assert result.uncited_source_items == 2
    assert result.decisions_source == "dropped-decisions.v1"
    assert result.correlation_id == recorded_command.correlation_id
    assert result.counts == {
        "changed_by_area": 5,
        "causes_and_fixes": 1,
        "decisions": 2,
        "open_blockers": 2,
        "do_not_rediscover": 3,
    }
    assert report.endswith("\n")
    assert not report.endswith("\n\n")


def test_technical_deep_dive_decisions_fall_back_to_ledger_rulings(
    recorded_command: ModelTechnicalDeepDiveCommand,
) -> None:
    command = recorded_command.model_copy(update={"dropped_decisions": None})
    result = HandlerTechnicalDeepDive().handle(command)
    decisions = _section(result.report_markdown, _HEADINGS[2])
    assert result.decisions_source == "ledger-rulings"
    assert (
        "Keep terminal evidence in the result envelope." not in result.report_markdown
    )
    assert "Preserve evidence during replay too." not in result.report_markdown
    assert (
        "Operator ruling recorded (kind=decision); read the ledger row for the words."
        in decisions
    )
    assert (
        "Restated: operator restated the ruling of 2026-10-23T08:00:00Z "
        "(kind=decision); read the ledger row for the words." in decisions
    )
    assert "Previous-day" not in result.report_markdown


@pytest.mark.parametrize(
    ("row", "expected", "area"),
    [
        (
            "RULING | kind=scope | ticket=OMN-1 | Sensitive operator words",
            "Operator ruling recorded (kind=scope); read the ledger row for the words.",
            "OMN-1",
        ),
        (
            "RULING | kind=scope | amends=`2026-10-23T08:00:00Z` | "
            "amends=ignored | Sensitive operator words",
            "Restated: operator restated the ruling of 2026-10-23T08:00:00Z "
            "(kind=scope); read the ledger row for the words.",
            "general",
        ),
        (
            "OPERATOR-CONSENT | ticket=OMN-1 | Sensitive operator words",
            "Operator consent recorded; read the ledger row for its scope.",
            "OMN-1",
        ),
    ],
)
def test_technical_deep_dive_operator_rows_use_pointers(
    row: str,
    expected: str,
    area: str,
) -> None:
    command = ModelTechnicalDeepDiveCommand(
        correlation_id=uuid4(),
        report_date=_DAY,
        owner_summary_facts={},
        ledger_rows=(f"{_DAY}T12:00:00Z | {row} | lane=lane-a",),
    )
    result = HandlerTechnicalDeepDive().handle(command)
    assert "Sensitive operator words" not in result.report_markdown
    assert len(result.claims) == 1
    assert result.claims[0].text == expected
    assert result.claims[0].area == area
    assert result.claims[0].section == EnumReportSection.DECISIONS


@pytest.fixture
def redaction_command() -> ModelTechnicalDeepDiveCommand:
    """Supply planted words and a branch prefix in a cited friction row."""
    return ModelTechnicalDeepDiveCommand(
        correlation_id=uuid4(),
        report_date=_DAY,
        owner_summary_facts={},
        ledger_rows=(
            f"{_DAY}T12:00:00Z | FRICTION | lane=zed-onboard-1 | class=Zed | "
            "`Zed` retried zed/omn-1-x; unzed stays unchanged.",
        ),
        redact_terms=("zed",),
    )


def test_technical_deep_dive_redacts_listed_terms(
    redaction_command: ModelTechnicalDeepDiveCommand,
) -> None:
    result = HandlerTechnicalDeepDive().handle(redaction_command)
    report = result.report_markdown
    assert "Zed" not in report
    assert "zed/" not in report
    assert "a teammate" in report
    assert "branch omn-1-x" in report
    assert "unzed stays unchanged" in report
    assert result.claims[0].area == "a teammate"
    assert result.claims[0].text.startswith("a teammate retried branch omn-1-x")
    ledger = result.claims[0].citation.ledger
    assert ledger is not None
    assert ledger.lane == "teammate-onboard-1"


def test_technical_deep_dive_cli_redact_terms(
    redaction_command: ModelTechnicalDeepDiveCommand,
    tmp_path: Path,
) -> None:
    facts = tmp_path / "facts.json"
    facts.write_text("{}", encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(redaction_command.ledger_rows), encoding="utf-8")
    terms = tmp_path / "terms.txt"
    terms.write_text("# Redaction terms\n\n  zed  # planted term\n", encoding="utf-8")
    out = tmp_path / "report.md"
    claims_out = tmp_path / "claims.json"
    assert (
        cli.main(
            [
                "technical",
                "--date",
                _DAY,
                "--facts",
                str(facts),
                "--ledger",
                str(ledger),
                "--redact-terms",
                str(terms),
                "--out",
                str(out),
                "--claims-out",
                str(claims_out),
            ]
        )
        == 0
    )
    for path in (out, claims_out):
        content = path.read_text(encoding="utf-8")
        assert "Zed" not in content
        assert "zed/" not in content
        assert "a teammate" in content
        assert "branch omn-1-x" in content


def test_technical_deep_dive_citation_check_refuses_planted_uncited_claim(
    recorded_command: ModelTechnicalDeepDiveCommand,
) -> None:
    claim = _uncited_claim()
    with pytest.raises(UncitedClaimError) as caught:
        render_technical_report(_DAY, [claim], 0, "ledger-rulings", [])
    assert tuple(refusal.claim for refusal in caught.value.refused) == (claim,)
    assert caught.value.refused[0].reason == "no commit sha, pull request or ledger row"
    claims, _ = build_claims(recorded_command)
    assert check_citations(claims) == ()


def test_technical_deep_dive_cli_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    facts = tmp_path / "facts.json"
    facts.write_text(
        (_FIXTURES / "recorded_day_facts.json").read_text(encoding="utf-8")
        + "\nFAIL optional source\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.md"
    claims_out = tmp_path / "claims.json"
    args = [
        "technical",
        "--date",
        _DAY,
        "--facts",
        str(facts),
        "--out",
        str(out),
        "--claims-out",
        str(claims_out),
    ]
    claim = _uncited_claim()
    with monkeypatch.context() as patched:
        patched.setattr(
            handler_technical_deep_dive, "build_claims", lambda _: ([claim], 0)
        )
        assert cli.main(args) == 2
    assert "REFUSED decisions Planted unsupported statement" in capsys.readouterr().out
    assert not out.exists()
    assert not claims_out.exists()

    ledger = str(_FIXTURES / "recorded_day_ledger.md")
    args.extend(
        [
            "--dropped-decisions",
            str(_FIXTURES / "recorded_day_dropped_decisions.json"),
            "--ledger",
            ledger,
            ledger,
            "--ledger",
            ledger,
        ]
    )
    assert cli.main(args) == 0
    assert capsys.readouterr().out == (
        f"TECHNICAL-DEEP-DIVE date={_DAY} claims=13 uncited_left_out=2 "
        f"decisions_from=dropped-decisions.v1 out={out}\n"
    )
    report = out.read_text(encoding="utf-8")
    assert report.endswith("\n")
    assert not report.endswith("\n\n")
    assert len(json.loads(claims_out.read_text(encoding="utf-8"))) == 13


def test_technical_deep_dive_scrubs_home_paths() -> None:
    prefixes = ["/" + root + "/" for root in ("Users", "home")]
    source = "see " + prefixes[0] + "operator/Code/x and " + prefixes[1] + "operator/y"
    cleaned = scrub(source)
    assert all(prefix not in cleaned for prefix in prefixes)
    assert cleaned == "see ~/Code/x and ~/y"
    assert scrub("  `quoted`\n\t prose  ") == "quoted prose"
    assert scrub("abcdef", 3) == "abc…"


def test_technical_deep_dive_parses_ledger_grammar() -> None:
    stamp = f"{_DAY}T12:00:00Z"
    row = parse_ledger_row(
        f"{stamp} | MSG | from=lane-a | ticket=OMN-1 | ticket=OMN-2 | "
        "first prose | invalid2=value | final prose\ncontinuation"
    )
    assert row is not None
    assert row.ts == stamp
    assert row.row_type == "MSG"
    assert row.lane == "lane-a"
    assert row.cells == {"from": "lane-a", "ticket": "OMN-1"}
    assert row.text == "final prose\ncontinuation"
    assert parse_ledger_row("not a timestamp | RULING | prose") is None
    assert (
        rows_for_day(
            [
                f"{_DAY}T23:00:00Z | STATUS | lane=lane-b",
                "2026-10-22T23:00:00Z | RULING | lane=lane-old",
                f"{stamp} | CLAIM | lane=lane-a",
                "header",
            ],
            _DAY,
        )[0].ts
        == stamp
    )


def test_technical_deep_dive_earliest_ledger_reference_and_deduplication() -> None:
    command = ModelTechnicalDeepDiveCommand(
        correlation_id=uuid4(),
        report_date=_DAY,
        owner_summary_facts={
            "merged_changes": [
                {"repo": "omnimarket", "number": 1, "title": "revert: stale schema"}
            ]
            * 2,
            "lanes_closed": [
                {"ts": f"{_DAY}T14:00:00Z", "lane": "lane-c", "pr": "omnimarket#1"},
                {"ts": f"{_DAY}T13:00:00Z", "lane": "lane-b", "pr": "omnimarket#1"},
            ],
        },
        ledger_rows=(
            f"{_DAY}T12:00:00Z | STATUS | lane=lane-a | pr=omnimarket#1 | Done",
            f"{_DAY}T15:00:00Z | TERMINAL | lane=lane-d | pr=omnimarket#1 | Done",
        ),
    )
    claims, uncited = build_claims(command)
    assert len(claims) == 2
    assert uncited == 0
    assert all(
        claim.citation.ledger == ModelLedgerRef(ts=f"{_DAY}T12:00:00Z", lane="lane-a")
        for claim in claims
    )


def test_technical_deep_dive_tolerates_dropped_shapes_and_invalid_references() -> None:
    command = ModelTechnicalDeepDiveCommand(
        correlation_id=uuid4(),
        report_date=_DAY,
        owner_summary_facts={},
        dropped_decisions={
            "decisions": [
                {
                    "quote": "Nested ruling",
                    "row": {"ts": f"{_DAY}T08:00:00Z", "lane": "lane-a"},
                },
                {
                    "statement": "Commit ruling",
                    "at": f"{_DAY}T09:00:00Z",
                    "commit": "abc1234",
                    "pr": "invalid",
                },
                {
                    "summary": "PR ruling",
                    "decided_at": f"{_DAY}T10:00:00Z",
                    "pr": ["omnimarket#1", "omnimarket#2"],
                },
                {
                    "decision": "Uncited ruling",
                    "ts": f"{_DAY}T11:00:00Z",
                    "sha": "invalid",
                    "pr": "invalid",
                },
                {"title": "Undated PR ruling", "pr": "omnimarket#3"},
                None,
            ],
            "findings": [
                {"text": "Undated commit finding", "sha": "abcdef0"},
                {
                    "text": "Old finding",
                    "ts": "2026-10-22T10:00:00Z",
                    "pr": "omnimarket#4",
                },
            ],
            "re_steers": "invalid list",
        },
    )
    claims, uncited = build_claims(command)
    assert [claim.text for claim in claims] == [
        "Nested ruling",
        "Commit ruling",
        "PR ruling",
        "Undated commit finding",
    ]
    assert uncited == 1
    assert check_citations(claims) == ()


def test_technical_deep_dive_empty_day_and_explicit_empty_dropped_data() -> None:
    result = HandlerTechnicalDeepDive().handle(
        ModelTechnicalDeepDiveCommand(
            correlation_id=uuid4(),
            report_date="2026-12-31",
            owner_summary_facts={},
            dropped_decisions={},
        )
    )
    assert result.report_markdown.count("- None recorded for 2026-12-31.") == 5
    assert "starting work on 2027-01-01" in result.report_markdown
    assert result.decisions_source == "dropped-decisions.v1"
    assert result.uncited_source_items == 0


@pytest.mark.parametrize(
    "missing_option", ["--facts", "--dropped-decisions", "--ledger", "--redact-terms"]
)
def test_technical_deep_dive_cli_missing_input(
    missing_option: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.md"
    args = [
        "technical",
        "--date",
        _DAY,
        "--facts",
        str(_FIXTURES / "recorded_day_facts.json"),
        "--out",
        str(out),
    ]
    missing = str(tmp_path / "missing.json")
    if missing_option == "--facts":
        args[args.index("--facts") + 1] = missing
    else:
        args.extend([missing_option, missing])
    assert cli.main(args) == 1
    assert "error:" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    "citation_data",
    [{"commit_sha": "xyz1234"}, {"commit_sha": "abc"}, {"pr": "invalid#x"}],
)
def test_technical_deep_dive_models_validate_citations(
    citation_data: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelClaimCitation.model_validate(citation_data)


def test_technical_deep_dive_models_are_frozen_and_render_all_references() -> None:
    citation = ModelClaimCitation(
        pr="omnimarket#4332",
        commit_sha="abc1234",
        ledger=ModelLedgerRef(ts=f"{_DAY}T12:00:00Z", lane="lane-a"),
    )
    assert citation.is_cited
    assert citation.render() == (
        "`omnimarket#4332` · `abc1234` · ledger `2026-10-23T12:00:00Z` lane `lane-a`"
    )
    with pytest.raises(ValidationError):
        citation.pr = "omnimarket#1"
    with pytest.raises(ValidationError):
        ModelLedgerRef(ts="invalid", lane="lane-a")
    with pytest.raises(ValidationError):
        ModelLedgerRef(ts=f"{_DAY}T12:00:00Z", lane="")
    with pytest.raises(ValidationError):
        ModelClaimCitation.model_validate({"unexpected": True})


def test_technical_deep_dive_scrubs_registry_root_paths() -> None:
    root = "omni" + "_home"
    sources = [
        "~/Code/" + root + "/omni_worktrees/x/repo",
        "/" + "Users/operator/Code/" + root + "/.onex_state/a.json",
        "$" + "OMNI_HOME/omnimarket",
        root + "/.claude_scratch/b",
    ]
    for source in sources:
        cleaned = scrub("kept " + source)
        assert root not in cleaned
        assert "OMNI_HOME" not in cleaned
        assert cleaned.startswith("kept the registry root/")
