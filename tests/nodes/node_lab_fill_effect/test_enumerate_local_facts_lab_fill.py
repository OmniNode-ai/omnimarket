# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local adapters of the approved-work and sprint-ticket enumerate operations (OMN-23250), run on real files, a real git repo and a scripted Linear."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillApprovedWork,
    HandlerLabFillSprintTickets,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillApprovedWorkRequest,
    ModelLabFillSprintTicketsRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillPortError,
    LabFillSourceCutError,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_enumerate_facts import (
    FencesTool,
    GitMergedHistory,
    LinearApprovedTickets,
    LinearSprintIssues,
    LocalApprovedRows,
    PathAcceptanceProbe,
    WatcherApprovedPrFacts,
    WatcherSprintPrs,
)

NOW = 1_790_000_000.0
STAMP = "2026-09-21T14:00:00Z"  # 13 min before NOW


def _state(tmp_path: Path, **over: object) -> Path:
    state: dict[str, object] = {
        "schema": 1,
        "operator": "op",
        "repos": ["omnimarket"],
        "last_tick": STAMP,
        "last_full_resync": STAMP,
        "prs": {
            "omnimarket#1": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 1,
                    "state": "OPEN",
                    "title": "fix(OMN-1): a",
                    "head_sha": "h1",
                    "head_ref": "omn-1-a",
                    "labels": [],
                    "updated_at": "2026-09-20T00:00:00Z",
                },
                "ci": {"sha": "h1", "verdict": "RED"},
            },
            "omnimarket#2": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 2,
                    "state": "MERGED",
                    "title": "feat(OMN-2): b",
                },
            },
            "occ#3": {
                "facts": {
                    "repo": "occ",
                    "number": 3,
                    "state": "MERGED",
                    "title": "evidence(OMN-9): c",
                },
            },
            "omnimarket#4": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 4,
                    "state": "OPEN",
                    "draft": True,
                    "title": "x(OMN-4)",
                    "head_sha": "h",
                },
            },
        },
    }
    state.update(over)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_rows_adapter_reads_declared_rows_and_refuses_a_bad_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approved.json"
    path.write_text(json.dumps({"version": 1, "rows": [{"id": "a"}, "junk"]}))
    assert LocalApprovedRows()(str(path)) == [{"id": "a"}, "junk"]
    path.write_text(json.dumps({"version": 2, "rows": []}))
    with pytest.raises(LabFillPortError, match="invalid approved-list"):
        LocalApprovedRows()(str(path))
    with pytest.raises(LabFillPortError, match="unreadable"):
        LocalApprovedRows()(str(tmp_path / "missing.json"))


def test_watcher_pr_facts_split_merged_open_and_skip_companions(tmp_path: Path) -> None:
    facts = WatcherApprovedPrFacts(
        state_path=_state(tmp_path),
        max_tick_age_min=20,
        max_resync_age_min=90,
        companion_repositories=["occ"],
        companion_title_prefix="evidence(",
        now=lambda: NOW,
    )()
    assert facts.merged_tickets == {"OMN-2"}
    assert dict(facts.open_tickets) == {
        "OMN-1": ("omnimarket#1",),
        "OMN-4": ("omnimarket#4",),
    }


def test_error_chain_stale_watcher_names_unreadable_from_snapshot(
    tmp_path: Path,
) -> None:
    adapter = WatcherApprovedPrFacts(
        state_path=_state(tmp_path),
        max_tick_age_min=5,
        max_resync_age_min=90,
        companion_repositories=[],
        companion_title_prefix="evidence(",
        now=lambda: NOW,
    )
    with pytest.raises(LabFillPortError, match="unreadable-from-snapshot"):
        adapter()


def test_sprint_prs_carry_head_and_ci_facts_and_skip_drafts(tmp_path: Path) -> None:
    prs = WatcherSprintPrs(
        state_path=_state(tmp_path),
        max_tick_age_min=20,
        max_resync_age_min=90,
        now=lambda: NOW,
    )(["omnimarket"])
    assert [(p.number, p.head_read, p.ci_head_matches, p.ci_verdict) for p in prs] == [
        (1, True, True, "RED")
    ]
    assert prs[0].idle_hours > 24


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def _clone(root: Path, name: str, subjects: list[str]) -> None:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    for subject in subjects:
        _git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            subject,
        )


def test_merged_history_reads_newest_implementation_and_names_an_unread_clone(
    tmp_path: Path,
) -> None:
    _clone(tmp_path, "omnimarket", ["feat(OMN-7): built", "evidence(OMN-7): companion"])
    adapter = GitMergedHistory(
        root=tmp_path,
        repos=["omnimarket", "absent"],
        base_refs=["origin/dev", "dev"],
        days=365,
        companion_prefix="evidence(",
    )
    history = adapter(["OMN-7", "OMN-8"], 30.0)
    assert history.newest["OMN-7"][0] == "omnimarket"
    assert "OMN-8" not in history.newest
    assert history.unread_repos == {"absent"}
    with pytest.raises(LabFillPortError, match="unreadable"):
        GitMergedHistory(
            root=tmp_path,
            repos=["absent"],
            base_refs=["dev"],
            days=1,
            companion_prefix="x",
        )(["OMN-7"], 30.0)


def test_error_chain_merged_history_cut_by_its_budget(tmp_path: Path) -> None:
    _clone(tmp_path, "omnimarket", ["feat(OMN-7): built"])
    with pytest.raises(LabFillSourceCutError):
        GitMergedHistory(
            root=tmp_path,
            repos=["omnimarket"],
            base_refs=["dev"],
            days=1,
            companion_prefix="x",
        )(["OMN-7"], 0.0)


def test_fences_tool_runs_a_real_child_and_a_slow_one_is_cut(tmp_path: Path) -> None:
    ok = FencesTool(
        argv=[sys.executable, "-c", 'print(\'{"fenced_tickets": ["OMN-5"]}\')'], env={}
    )
    assert ok(10.0) == ("OMN-5",)
    slow = FencesTool(argv=[sys.executable, "-c", "import time; time.sleep(5)"], env={})
    with pytest.raises(LabFillSourceCutError):
        slow(0.5)
    bad = FencesTool(argv=[sys.executable, "-c", "print('{}')"], env={})
    with pytest.raises(LabFillPortError, match="fenced_tickets"):
        bad(10.0)


def test_acceptance_probe_finds_commands_and_paths(tmp_path: Path) -> None:
    (tmp_path / "omnimarket" / "tests").mkdir(parents=True)
    (tmp_path / "omnimarket" / "tests" / "a.py").write_text("")
    probe = PathAcceptanceProbe(roots=[tmp_path])
    assert probe("python3", "omnimarket")
    assert probe("tests/a.py", "omnimarket")
    assert not probe("tests/missing.py", "omnimarket")
    assert not probe("no-such-tool-xyz", "omnimarket")


class ScriptedLinear:
    """Answers each query by the marker it carries; records the queries."""

    def __init__(self, answers: Mapping[str, list[dict[str, object]]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.queries: list[str] = []

    def __call__(self, query: str, budget_s: float) -> Mapping[str, object]:
        self.queries.append(query)
        for marker, pages in self.answers.items():
            if marker in query:
                return pages.pop(0)
        raise LabFillPortError("no scripted answer")


def _issue(n: int, **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": f"id{n}",
        "identifier": f"OMN-{n}",
        "title": f"t{n}",
        "updatedAt": STAMP,
        "state": {"type": "started"},
        "labels": {"nodes": [{"name": "lab"}]},
        "projectMilestone": {"name": "M1"},
        "children": {"nodes": []},
        "project": {"id": "proj"},
        "assignee": None,
        "description": "Repo: omnimarket.",
        "attachments": {
            "nodes": [
                {
                    "url": "https://github.com/o/omnimarket/pull/9",
                    "createdAt": STAMP,
                    "sourceType": "github",
                    "title": "feat(x): y",
                    "metadata": {"status": "merged"},
                }
            ]
        },
    }
    base.update(over)
    return base


def test_linear_sprint_issues_pages_and_reads_the_node_shape() -> None:
    linear = ScriptedLinear(
        {
            "project:{id:{eq:": [
                {
                    "issues": {
                        "nodes": [_issue(1)],
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    }
                },
                {
                    "issues": {
                        "nodes": [_issue(2, children={"nodes": [{"id": "x"}]})],
                        "pageInfo": {"hasNextPage": False},
                    }
                },
            ]
        }
    )
    nodes = LinearSprintIssues(
        query=linear, team_key="OMN", fleet_repos=["omnimarket"]
    ).project("proj", ["started"], 30.0)
    assert [n.ticket for n in nodes] == ["OMN-1", "OMN-2"]
    assert 'after:"c1"' in linear.queries[1]
    first = nodes[0]
    assert (first.state_type, first.milestone, first.description_repo, first.prs) == (
        "started",
        "M1",
        "omnimarket",
        ("omnimarket#9",),
    )
    assert first.merged_implementations == ("omnimarket#9",)
    assert nodes[1].has_children
    assert nodes[1].merged_implementations == ()


def test_chain_adapters_through_both_handlers(tmp_path: Path) -> None:
    """The handlers run on the real adapters: approved rows from a file, history from a git repo, Linear scripted."""
    _clone(tmp_path, "omnimarket", ["feat(OMN-12): built long ago"])
    approved = tmp_path / "approved.json"
    approved.write_text(
        json.dumps(
            {
                "version": 1,
                "rows": [
                    {
                        "id": "r1",
                        "kind": "process-fix",
                        "ticket": "OMN-11",
                        "repo": "omnimarket",
                        "goal": "g",
                        "acceptance_check": "python3 -m pytest",
                        "added_by": "x",
                        "added_at": "2026-09-01T00:00:00Z",
                    },
                    {
                        "id": "r2",
                        "kind": "wiring",
                        "ticket": "OMN-12",
                        "repo": "omnimarket",
                        "goal": "g2",
                        "acceptance_check": "python3 -m pytest",
                        "added_by": "x",
                        "added_at": "2026-09-01T00:00:00Z",
                    },
                ],
            }
        )
    )
    linear = ScriptedLinear(
        {"number:{in:": [{"issues": {"nodes": [_issue(11), _issue(12)]}}]}
    )
    handler = HandlerLabFillApprovedWork(
        rows=LocalApprovedRows(),
        prs=WatcherApprovedPrFacts(
            state_path=_state(tmp_path),
            max_tick_age_min=20,
            max_resync_age_min=90,
            companion_repositories=["occ"],
            companion_title_prefix="evidence(",
            now=lambda: NOW,
        ),
        history=GitMergedHistory(
            root=tmp_path,
            repos=["omnimarket"],
            base_refs=["dev"],
            days=3650,
            companion_prefix="evidence(",
        ),
        tickets=LinearApprovedTickets(query=linear, team_key="OMN"),
        acceptance=PathAcceptanceProbe(roots=[tmp_path]),
    )
    result = handler.handle(
        ModelLabFillApprovedWorkRequest(
            path=str(approved),
            kinds=("process-fix", "partial-node", "wiring"),
            done_bases=("duplicate",),
            fleet_repos=("omnimarket",),
            max_rows=500,
            budget_s=60.0,
            stage_floor_s=5.0,
        )
    )
    assert result.state == "ok"
    skips = {c.row["ticket"]: c.approved_skip for c in result.candidates}
    assert skips["OMN-11"] == ""
    assert skips["OMN-12"].startswith(
        "stale:merged-after-check:omnimarket@"
    )  # merged after the row was added
    assert "python3" in result.candidates[0].acceptance_verified

    sprint_linear = ScriptedLinear(
        {
            "project:{id:{eq:": [
                {"issues": {"nodes": [_issue(1)], "pageInfo": {"hasNextPage": False}}}
            ],
            "labels:{name:{in:": [
                {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
            ],
        }
    )
    sprint = HandlerLabFillSprintTickets(
        issues=LinearSprintIssues(
            query=sprint_linear, team_key="OMN", fleet_repos=["omnimarket"]
        ),
        watcher=WatcherSprintPrs(
            state_path=_state(tmp_path),
            max_tick_age_min=20,
            max_resync_age_min=90,
            now=lambda: NOW,
        ),
        fences=FencesTool(
            argv=[sys.executable, "-c", "print('{\"fenced_tickets\": []}')"], env={}
        ),
    ).handle(
        ModelLabFillSprintTicketsRequest(
            project_id="proj",
            ticket_state_types=("backlog", "unstarted", "started"),
            defect_state_types=("unstarted", "started"),
            defect_labels=("defect",),
            defect_terms=(),
            pr_repos=("omnimarket",),
            stalled_hours=24.0,
            budget_s=60.0,
            stage_floor_s=5.0,
        )
    )
    assert sorted((c.kind, c.ticket) for c in sprint.candidates) == [
        ("pr-red", "OMN-1"),
        ("ticket", "OMN-1"),
    ]
    assert sprint.stale == ()
    assert sprint.unreadable == ()
