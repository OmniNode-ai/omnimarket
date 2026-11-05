# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the November 1 report: 30/120 days hid plans dated October 2-8."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import (
    base,
    linear_tools,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.context import (
    ModelMorningToolContext,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.server import (
    HandlerMorningToolServer,
)

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_plan_ticket_states_reads_every_plan_before_filtering_and_paging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate fixture Git reads from the remote runner's worktree pointers.
    for key in os.environ.keys() - scrub_git_location_env(os.environ).keys():
        monkeypatch.delenv(key)
    repo = tmp_path / "kb"
    plans = repo / "beta" / "plans"
    plans.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    dated: dict[str, date] = {}
    for age in range(-1, 64):
        minted = date(2026, 11, 1) - timedelta(days=age)
        for slot in range(2):
            name = f"{minted}-{'reconciliation-' * 4}{slot}.md"
            dated[name] = minted
            (plans / name).write_text(f"# Plan\n\nOMN-{slot + 1}\n")
    for day in ("2026-07-03", "2026-07-04"):
        name = f"{day}-boundary.md"
        dated[name] = date.fromisoformat(day)
        (plans / name).write_text("# Plan\n\nOMN-1\n")
    (plans / "2026-02-30-invalid.md").write_text("invalid date\n")
    (plans / "README.md").write_text("not a dated plan\n")
    _git(repo, "add", "beta/plans")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    listing = _git(
        repo, "ls-tree", "--name-only", "refs/remotes/origin/main:beta/plans"
    )
    assert len(listing) > base.OUTPUT_CHARS

    def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        assert "issues(" in query
        assert variables["archived"] is True
        assert variables["filter"]["team"] == {"key": {"eq": "OMN"}}
        return {
            "issues": {
                "nodes": [
                    {"identifier": f"OMN-{n}", "state": {"type": "started"}}
                    for n in variables["filter"]["number"]["in"]
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }

    monkeypatch.setattr(linear_tools, "_graphql", graphql)
    ctx = ModelMorningToolContext(
        phase_label="dropped-work",
        date="2026-11-01",
        publish=False,
        root=tmp_path,
        ledger_path=tmp_path / "ledger.md",
        kb_clone=repo,
        kb_worktree=tmp_path / "worktree",
        kb_branch="automation/test",
        commit_prefix="test:",
        artifacts={},
        commands={},
    )
    server = HandlerMorningToolServer(ctx)
    totals = []
    for days in (30, 120):
        expected = {
            name
            for name, minted in dated.items()
            if 0 <= (date(2026, 11, 1) - minted).days <= days
        }
        seen: list[str] = []
        offset = 0
        while True:
            ok, text = server.call(
                "plan_ticket_states", {"days": days, "offset": offset}
            )
            assert ok, text
            result = json.loads(text)
            assert result["total_plans"] == len(expected)
            assert result["offset"] == offset
            assert len(result["plans"]) <= 60
            for row in result["plans"]:
                assert row["resolved"] == row["cited"] == row["in_progress"] == 1
                assert row["dangling"] == []
                seen.append(row["plan"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        assert len(seen) == len(set(seen))
        assert set(seen) == expected
        assert {
            name
            for name, minted in dated.items()
            if date(2026, 10, 2) <= minted <= date(2026, 10, 8)
        } <= set(seen)
        totals.append(len(seen))
    assert totals[1] > totals[0]


def test_git_command_report_stays_bounded(tmp_path: Path) -> None:
    repo = tmp_path / "kb"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    expected = _git(repo, "help", "-a").strip()
    assert len(expected) > base.OUTPUT_CHARS
    assert base._git_out(repo, "help", "-a") == expected
    report = base._git(repo, "help", "-a")
    assert report["exit"] == 0
    assert report["stdout"] == "..." + (expected + "\n")[-base.OUTPUT_CHARS :]
    assert base._git_out(repo, "show", "refs/remotes/origin/missing") is None
