# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded reviewer findings respect explicitly counted omissions."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_owner_summary_effect.__main__ import main
from omnimarket.nodes.node_owner_summary_effect.handlers import HandlerOwnerSummary
from omnimarket.nodes.node_owner_summary_effect.models import (
    ModelMergedChange,
    ModelOwnerSummaryFacts,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_closed import (
    ModelLaneClosed,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_m4_row import ModelM4Row

pytestmark = pytest.mark.unit
HANDLER = HandlerOwnerSummary()
ANSWER = Path(__file__).parent / "fixtures/recorded_revise_findings.md"
END = "2026-10-24T04:02:25Z"


def _facts() -> ModelOwnerSummaryFacts:
    changes = (
        (
            "omnimarket",
            4338,
            "worker owns inference client; refuse non-SQLite claims store",
            ("OMN-19174",),
        ),
        (
            "omnimarket",
            4337,
            "capture silence watch over ledger principals and hook hosts",
            (),
        ),
        (
            "omnibase_core",
            1029,
            "declare the twice-daily owner-summary tick in the host installer contract",
            (),
        ),
        (
            "omnibase_infra",
            2964,
            "the advisory-job gate reads EXTERNAL_SKIPPABLE_CONTEXTS keys as enforced",
            (),
        ),
    )
    lanes = (
        (
            "fill-eval-run-variance-repeat-a93ea0c41402-2-20261023T2339Z",
            "",
            "done",
            "30 of 30 success",
        ),
        (
            "fill-eval-run-variance-repeat-a93ea0c41402-3-20261024T0120Z",
            "",
            "done",
            "30 of 30 success",
        ),
        (
            "floor-diagnose-omniclaude-20261024T0237Z",
            "",
            "done",
            "cause no-supply-plus-latency, not a stalled controller",
        ),
        (
            "fix-local-store-claims-index-2345",
            "OMN-21349",
            "blocked",
            "refused work-log registration",
        ),
        ("cmod-next-2305", "OMN-21099", "partial", "partial work"),
        ("cmod-blocked", "OMN-21099", "blocked", "blocked work"),
    )
    return ModelOwnerSummaryFacts(
        generated_at=END,
        window_since="2026-10-23T16:02:25Z",
        window_until=END,
        window_source="--since",
        full_list_link="https://example.invalid/full-list",
        merged_changes=tuple(
            ModelMergedChange(repo=r, number=n, title=t, tickets=ids, merged_at=END)
            for r, n, t, ids in changes
        ),
        merged_by_repo={"omnimarket": 2, "omnibase_core": 1, "omnibase_infra": 1},
        lanes_closed=tuple(
            ModelLaneClosed(
                ts=END, lane=lane, ticket=ticket, outcome=outcome, text=text
            )
            for lane, ticket, outcome, text in lanes
        ),
        lanes_closed_count=6,
        lanes_closed_by_outcome={"done": 3, "blocked": 2, "partial": 1},
        m4_rows=(
            ModelM4Row(
                ts=END, row_type="STATUS", lane="milestones", ticket="OMN-21349"
            ),
        ),
    )


def _draft() -> str:
    return (
        "4 changes merged across 3 repositories. The window ends Saturday October 24 at 12:02 AM ET.\n"
        "omnimarket#4337 changed monitoring. Merges are unconfirmed as running.\n"
        "6 work runs (lanes) closed: 3 done, 2 blocked, 1 partial.\n"
        "M4 board (milestones): OMN-21349 blocked by refused work-log registration.\n"
        "OMN-21099 has blocked and partial work. Friction data is unavailable.\n"
        f"Not listed: 3 merged changes (3 other changes), 1 ticket, 3 closed work runs (3 as done). Full list: {HANDLER.coverage_of(_facts()).full_list_link}\n"
        "(via Owner's agent)\n"
    )


def test_sound_draft() -> None:
    assert len(_draft().split()) < 420
    assert HANDLER.check_draft(_draft(), _facts()) == []


def test_recorded_findings() -> None:
    results = HANDLER.triage_findings(ANSWER.read_text(), _draft(), _facts())
    assert [label for label, _ in results] == [
        "NAMED-OMISSION",
        "NAMED-OMISSION",
        "NAMED-OMISSION",
        "NAMED-OMISSION",
        "FINDING",
        "FINDING",
        "NAMED-OMISSION",
        "FINDING",
        "FINDING",
    ]


def test_omissions_need_exact_counts_and_link() -> None:
    absent = "\n".join(
        line for line in _draft().splitlines() if not line.startswith("Not listed:")
    )
    assert any("not named" in p for p in HANDLER.check_draft(absent, _facts()))
    wrong = _draft().replace("3 merged changes", "2 merged changes")
    assert any("unnamed omission" in p for p in HANDLER.check_draft(wrong, _facts()))
    no_link = _draft().replace(HANDLER.coverage_of(_facts()).full_list_link, "")
    assert any("full list link" in p for p in HANDLER.check_draft(no_link, _facts()))
    assert all(
        label == "FINDING"
        for label, _ in HANDLER.triage_findings(ANSWER.read_text(), wrong, _facts())
    )


def test_wrong_facts() -> None:
    assert any(
        "is not a merge in the facts" in p
        for p in HANDLER.check_draft(
            _draft().replace("omnimarket#4337", "omnimarket#4399"), _facts()
        )
    )
    finding = "The draft says 31 changes merged across 3 repositories; the facts give 29 across 4 (headline)."
    assert HANDLER.triage_findings(
        "VERDICT: REVISE\n" + finding, _draft(), _facts()
    ) == [("FINDING", finding)]


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        pytest.param(ANSWER.read_text(), 1, id="real-findings"),
        pytest.param(
            "\n".join(ANSWER.read_text().splitlines()[:5]), 0, id="named-omissions"
        ),
        pytest.param("VERDICT: APPROVE", 0, id="approve"),
    ],
)
def test_triage_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: str, code: int
) -> None:
    draft, facts, findings = (
        tmp_path / name for name in ("draft.md", "facts.json", "findings.md")
    )
    draft.write_text(_draft())
    facts.write_text(_facts().model_dump_json())
    findings.write_text(answer)
    assert (
        main(
            [
                "triage",
                "--draft",
                str(draft),
                "--facts",
                str(facts),
                "--findings",
                str(findings),
            ]
        )
        == code
    )
    output = capsys.readouterr().out
    assert "FAIL " not in output
    assert "triage:" in output
