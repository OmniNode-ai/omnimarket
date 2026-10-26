# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick must not send work behind the repository fixer HOLDs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as fill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import (
    ModelBacklogSources,
    ModelLabFillRequest,
)

NOW = "2026-10-26T19:30:00Z"
RULING = "RULING 2026-10-26T17:22:56Z lane=orchestrator-31d9"


@pytest.mark.parametrize("repo", ["repo-retired-data", "repo-retired-plugin"])
@pytest.mark.parametrize(
    "kind", ["dev-red", "process-fix", "cause-red", "m4-board-red"]
)
def test_tick_drops_held_repository_with_ruling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str, kind: str
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"2026-10-26T17:24:04Z | HOLD | id=retire | scope=fixer | repo={repo} | "
        f"until=2026-11-09T00:00:00Z | {RULING}\n"
    )
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    lines = {
        "dev-red": f"MISSING repo-status:{repo} | broken; base_branch_red: pytest",
        "process-fix": f"MISSING process-fix:{repo}#7 | unowned",
        "cause-red": f"MISSING repo-status:{repo} | shared_red_unowned: pytest",
    }
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "standing_tickets": {kind: "OMN-21396"},
                "items": [
                    {
                        "kind": kind,
                        "key": "stored-red",
                        "repo": repo,
                        "ticket": "OMN-21396",
                    }
                ]
                if kind == "m4-board-red"
                else [],
            }
        )
    )
    sent: list[list[str]] = []
    monkeypatch.setattr(fill, "_send", lambda argv: (sent.append(argv) is None, "sent"))
    request = ModelLabFillRequest(
        now=NOW,
        request={
            "lab_headroom": {
                "hosts": [{"name": "h201", "admission": {"claude_slots": 1}}]
            }
        },
        lines=[
            "MISSING lab-headroom:h201 | free Claude slots 1",
            *([lines[kind]] if kind in lines else []),
        ],
        launch=None,
        state_dir=tmp_path / "state",
        backlog=str(backlog),
        runner=tmp_path / "runner.py",
        out_of_scope={},
        lanes_dir=tmp_path / "lanes",
    )
    result = fill.HandlerLabFill().handle(request)
    assert sent == [], (
        "the current fill dispatches a lane despite the ledger fixer HOLD"
    )
    note = next(row[2] for row in result.found if row[1] == "lab-fill-dropped")
    assert repo in note
    assert "scope=fixer" in note
    assert RULING in note
    drops = json.loads((tmp_path / "state/lab-fill.dropped.json").read_text())
    assert any(RULING in drop["why"] for drop in drops.values())

    # Releasing this HOLD restores the candidate without changing any settings.
    with ledger.open("a") as stream:
        stream.write("2026-10-26T19:29:00Z | RELEASE | re=retire | lifted\n")
    fill.HandlerLabFill().handle(request)
    assert len(sent) == 1
    assert sent[0][sent[0].index("--repo") + 1] == repo


def test_ledger_rolls_expiry_release_and_arbitrary_repository(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ROLLING_WORK_LEDGER_old.md").write_text(
        "2026-09-01T00:00:00Z | HOLD | id=old | scope=fixer | repo=Owner/any-repository,still-held\n"
        "2026-09-02T00:00:00Z | HOLD | scope=fixer | repo=timestamp-release\n"
    )
    ledger.write_text(
        "2026-10-26T18:00:00Z | RELEASE | re=old\n"
        "2026-10-26T18:00:01Z | RELEASE | re=2026-09-02T00:00:00Z\n"
        "2026-10-26T18:00:02Z | HOLD | scope=fixer | repo=expired | until=2026-10-26T19:30:00Z\n"
        "2026-10-26T18:00:03Z | HOLD | scope=pr | repo=not-fixer\n"
        "2026-10-26T18:00:04Z | HOLD | id=live | scope=fixer | repo=Owner/any-repository\n"
        "2026-10-26T20:00:00Z | RELEASE | re=live\n"
    )
    holds, unread = fill.read_fixer_holds(NOW, {"ONEX_LEDGER_PATH": str(ledger)})
    assert not unread
    assert set(holds) == {"any-repository"}
    assert fill.fixer_hold_reason({"repo": "Owner/any-repository"}, holds)
    assert fill.fixer_hold_reason({"repo": "unrelated"}, holds) is None
    ledger.write_text("")
    holds, unread = fill.read_fixer_holds(NOW, {"OMNI_HOME": str(tmp_path / "missing")})
    assert holds == {}
    assert "FileNotFoundError" in unread


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("repo", ["", "repo-held"])
def test_generated_tickets_readbacks_and_designs_cannot_bypass_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only: bool, repo: str
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"2026-10-26T17:24:04Z | HOLD | scope=fixer | repo=repo-held | {RULING}\n"
    )
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    snapshot = tmp_path / "linear.json"
    snapshot.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "id": ticket,
                        "title": "repo-held delegation work",
                        "repo": repo,
                        "labels": ["delegation"],
                        "status": "Todo",
                    }
                    for ticket in ("OMN-301", "OMN-302")
                ]
            }
        )
    )
    scope = tmp_path / "scope.json"
    scope.write_text('{"out_of_scope": {}}')
    sources = ModelBacklogSources(
        linear_snapshot=snapshot,
        scope=scope,
        ledger_tickets=lambda: (set(), {"OMN-302": "missing readback"}),
        ticket_rule=fill.omnimarket_ticket_rule(),
        experiments=lambda: [
            {
                "id": "held",
                "title": "repo-held experiment",
                "repo": repo,
                "design": "run",
                "measure": "count",
                "ticket": "OMN-21396",
            }
        ],
    )
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {"items": [], "standing_tickets": {"experiment-design": "OMN-21396"}}
        )
    )
    sent: list[list[str]] = []
    monkeypatch.setattr(fill, "_send", lambda argv: (sent.append(argv) is None, "sent"))
    request = ModelLabFillRequest(
        now=NOW,
        request={
            "lab_headroom": {
                "hosts": [{"name": "h201", "admission": {"claude_slots": 1}}]
            }
        },
        lines=["MISSING lab-headroom:h201 | free Claude slots 1"],
        launch=None,
        state_dir=tmp_path / "state",
        backlog=str(backlog),
        runner=tmp_path / "runner.py",
        sources=sources,
        out_of_scope={},
        lanes_dir=tmp_path / "lanes",
        read_only=read_only,
    )
    result = fill.HandlerLabFill().handle(request)
    assert sent == []
    note = next(row[2] for row in result.found if row[1] == "lab-fill-dropped")
    assert "n=3" in note
    assert all(
        key in note
        for key in ("ticket:OMN-301", "readback:OMN-302", "exp:held", RULING)
    )
    if read_only:
        assert not (tmp_path / "state").exists()

    # An unread ledger also blocks repository work, including repo-less tickets.
    ledger.unlink()
    result = fill.HandlerLabFill().handle(request)
    assert sent == []
    assert any(row[1] == "lab-fill-fixer-holds" for row in result.found)
    assert "ledger unread" in next(
        row[2] for row in result.found if row[1] == "lab-fill-dropped"
    )


def test_hold_skips_do_not_consume_capacity_and_all_holds_cover_repository_work() -> (
    None
):
    request = fill.ModelIdleSlotFillRequest(
        now=NOW,
        hosts=[{"name": "h201", "admission": {"claude_slots": 1}}],
        lines=["MISSING lab-headroom:h201 | free Claude slots 1"],
        backlog=[
            {"kind": "ticket", "key": repo, "repo": repo, "ticket": "OMN-21396"}
            for repo in ("held", "allowed")
        ],
        fixer_holds={"held": "declared HOLD"},
    )
    result = fill.HandlerIdleSlotFill().handle(request)
    assert [item["repo"] for item in result.sends] == ["allowed"]
    result = fill.HandlerIdleSlotFill().handle(
        replace(request, fixer_holds={"all": "global HOLD"})
    )
    assert not result.sends
    assert len(result.dropped) == 2
