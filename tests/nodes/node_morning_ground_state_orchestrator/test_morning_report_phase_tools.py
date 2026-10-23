# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six report phases' deterministic tools (OMN-17427, RULING 2026-10-21T22:32:15Z).

Each tool reads only local sources or runs one overlay-named program with arguments it
builds itself; these tests drive them against fixtures: a PR watcher projection, a ledger,
a reports directory, and stand-in programs for the ledger append and the delegation step.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import (
    effects,
    linear_tools,
    local_reads,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.base import (
    MorningToolRefusalError,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.catalog import TOOLS
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.context import (
    ModelMorningToolContext,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.phases import (
    phase_lane,
    phase_note,
    phase_tools_contract,
)

REPORT_PHASES = (
    "ground-state",
    "morning-triage",
    "plan-reconcile",
    "integration-plan",
    "dropped-work",
    "session-goal",
)
SHA = "a" * 40


def _watcher(path: Path) -> None:
    state = {
        "last_tick": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_full_resync": "2026-10-22T00:00:00Z",
        "repos": {"omnidemo": {"default_branch": "dev"}},
        "settings": {
            "omnidemo": {
                "branches": {
                    "dev": {
                        "protection": "protected",
                        "contexts": ["CI Summary", "Merge Gate", "Only In Queue"],
                        "rule_contexts": [],
                    }
                },
                "rulesets": [],
            }
        },
        "branches": {
            "omnidemo@dev": {
                "sha": SHA,
                "checks": {
                    "read_at": "x",
                    "sha": SHA,
                    "total": 3,
                    "truncated": False,
                    "rows": [
                        [
                            "CI Summary",
                            "completed",
                            "failure",
                            "t1",
                            "t2",
                            "1",
                            "2",
                            "101",
                        ],
                        [
                            "Merge Gate",
                            "completed",
                            "success",
                            "t1",
                            "t2",
                            "3",
                            "4",
                            "102",
                        ],
                        [
                            "Only In Queue",
                            "completed",
                            "success",
                            "t1",
                            "t2",
                            "5",
                            "6",
                            "103",
                        ],
                    ],
                },
                "push": {
                    "rows": [
                        ["101", "CI", "completed", "failure", "t1", SHA],
                        ["102", "Gate", "completed", "success", "t1", SHA],
                    ]
                },
                "runs": {
                    "read_at": "x",
                    "rows": [
                        [
                            "103",
                            "9",
                            ".github/workflows/queue.yml",
                            "Queue",
                            "1",
                            "merge_group",
                            "completed",
                            "success",
                            "gh-readonly-queue/dev/pr-1",
                            "2026-10-22T01:00:00Z",
                        ],
                        [
                            "104",
                            "9",
                            ".github/workflows/sweep.yml",
                            "Sweep",
                            "1",
                            "schedule",
                            "completed",
                            "success",
                            "dev",
                            "2026-10-22T01:30:00Z",
                        ],
                    ],
                },
            }
        },
        "merges": {
            "omnidemo#7": {
                "merged_at": "2026-10-21T10:00:00Z",
                "merge_sha": SHA,
                "base": "dev",
                "title": "feat(OMN-1234): thing",
                "head_ref": "omn-1234-thing",
            }
        },
        "prs": {
            "omnidemo#9": {
                "facts": {"title": "fix(OMN-1234): more", "head_ref": "omn-1234-more"},
                "cls": "pending",
                "blocked": False,
                "queued": False,
            }
        },
    }
    path.write_text(json.dumps(state))


def _program(path: Path, body: str) -> Path:
    path.write_text(f"import sys, json, os\n{body}\n")
    return path


def _ctx(
    tmp_path: Path, label: str = "dropped-work", publish: bool = False
) -> ModelMorningToolContext:
    root = tmp_path / "root"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    ledger = root / "docs" / "ledger.md"
    if not ledger.exists():
        ledger.write_text("# ledger\n")
    watcher = tmp_path / "state.json"
    _watcher(watcher)
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    appended = tmp_path / "appended.txt"
    append = _program(
        tmp_path / "append.py",
        f"open({str(ledger)!r}, 'a').write(sys.argv[1] + '\\n'); "
        f"open({str(appended)!r}, 'a').write(sys.argv[1] + '\\n')",
    )
    delegation = _program(
        tmp_path / "delegation.py",
        "args = sys.argv[1:]\n"
        "if args[0] == 'text':\n"
        "    out = args[args.index('--out') + 1]\n"
        "    open(out, 'w').write('drafted prose')\n"
        "    print('RECEIPT /r/1.json'); print('DELEGATED step=s engine=glm run=run-1 model=m')\n"
        "elif args[0] == 'dispose':\n"
        "    print('DISPOSED ' + ' '.join(args))\n"
        "elif args[0] == 'cells':\n"
        "    print('delegated=1 runs=run-1 lab=0 glm=1')\n",
    )
    lane, ticket = phase_lane(label)
    return ModelMorningToolContext(
        phase_label=label,
        date="2026-10-22",
        publish=publish,
        root=root,
        ledger_path=ledger,
        kb_clone=tmp_path / "kb",
        kb_worktree=tmp_path / "worktree",
        kb_branch="automation/demo-2026-10-22",
        commit_prefix="docs(OMN-1):",
        artifacts={
            "dropped_work": "beta/tracking/2026-10-22-dropped-work.md",
            "goal": "beta/GOAL.md",
        },
        commands={
            "ledger_append_command": [sys.executable, str(append)],
            "delegation_step_command": [sys.executable, str(delegation)],
        },
        places={
            "pr_watcher_state": watcher,
            "reports_dir": reports,
            "clones_root": tmp_path / "clones",
            "process_readme": _readme(tmp_path),
            "venv_reconcile_status": _status(tmp_path),
            "venv_reconcile_log": _log(tmp_path),
        },
        lane=lane,
        ticket=ticket,
        model="claude-opus-5-5",
        state_dir=tmp_path / "run",
    )


def _readme(tmp_path: Path) -> Path:
    path = tmp_path / "README.md"
    path.write_text(
        "# Top\n\n## Dropped work - five\nbody one\n### sub\nbody two\n## Goal\ngoal body\n"
    )
    return path


def _status(tmp_path: Path) -> Path:
    path = tmp_path / "workspace-reconcile.status"
    path.write_text("clones/venv: in sync as of 2026-10-22T01:00:00Z\n")
    return path


def _log(tmp_path: Path) -> Path:
    path = tmp_path / "workspace-reconcile.log"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(f'{stamp} tick=complete reconciler_exit=0 verdict="in sync"\n')
    return path


def _run(name: str, ctx: ModelMorningToolContext, **args: Any) -> dict[str, Any]:
    tool = TOOLS[name]
    return cast(dict[str, Any], tool.run(ctx, tool.input_model.model_validate(args)))


@pytest.mark.unit
@pytest.mark.parametrize("label", REPORT_PHASES)
def test_every_report_phase_has_its_own_lane_ticket_and_tool_guide(label: str) -> None:
    lane, ticket = phase_lane(label)
    assert lane
    assert ticket
    assert ticket.startswith("OMN-")
    note = phase_note(label)
    assert "TOOL GUIDE FOR THIS PHASE" in note
    phases = cast(dict[str, dict[str, Any]], phase_tools_contract()["phases"])
    names = phases[label]["tools"]
    assert "ledger_append_claim" in names
    assert "delegate_prose" in names
    assert "delegation_cells" in names
    if label == "session-goal":
        assert {"kb_push_branch", "kb_open_pr", "pr_handoff"} <= set(names)
    assert not cast(dict[str, object], phase_tools_contract()["pending"])


@pytest.mark.unit
def test_a_gap_is_named_in_the_brief_as_unknown_never_zero() -> None:
    note = phase_note("dropped-work")
    assert "NO TOOL YET" in note
    assert "lab_pass_receipts_read" in note
    assert "never 0" in note


@pytest.mark.unit
def test_watcher_branch_head_attributes_check_runs_to_push_on_the_integration_branch(
    tmp_path: Path,
) -> None:
    out = _run("watcher_branch_head", _ctx(tmp_path), repo="omnidemo")
    verdicts = {r["context"]: r["verdict"] for r in out["required"]}
    assert verdicts == {
        "CI Summary": "FAILING",
        "Merge Gate": "PASSING",
        "Only In Queue": "ABSENT",
    }
    assert out["head_sha"] == SHA
    assert out["failing_push_workflows_at_head"] == ["CI"]
    with pytest.raises(MorningToolRefusalError, match="not a repository"):
        _run("watcher_branch_head", _ctx(tmp_path), repo="unknown_repo")


@pytest.mark.unit
def test_watcher_reads_merges_open_prs_and_workflow_runs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    merged = _run("pull_merge_state", ctx, repo="omnidemo", pr=7)
    assert merged["state"] == "MERGED"
    assert merged["merge_sha"] == SHA
    assert _run("pull_merge_state", ctx, repo="omnidemo", pr=9)["state"] == "OPEN"
    prs = _run("ticket_prs", ctx, ticket="OMN-1234")
    assert [p["pr"] for p in prs["open"]] == ["omnidemo#9"]
    assert [p["pr"] for p in prs["merged"]] == ["omnidemo#7"]
    assert _run("ticket_prs", ctx, ticket="OMN-123")["merged"] == []
    runs = _run("watcher_workflow_runs", ctx, repo="omnidemo", workflow="sweep.yml")
    assert [r["run_id"] for r in runs["runs"]] == ["104"]


@pytest.mark.unit
def test_open_claims_and_holds_come_from_the_ledger(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ctx.ledger_path.write_text(
        "# ledger\n"
        f"{now} | CLAIM | lane=open-lane | ticket=OMN-1 | x\n"
        f"{now} | CLAIM | lane=done-lane | ticket=OMN-2 | x\n"
        f"{now} | TERMINAL | lane=done-lane | friction=none | x\n"
        f"{now} | HOLD | from=op | to=lane | id=hold-1 | hold\n"
    )
    out = _run("ledger_open_claims", ctx, since_hours=24)
    assert [c["lane"] for c in out["open_claims"]] == ["open-lane"]
    assert [h["id"] for h in out["open_holds"]] == ["hold-1"]


@pytest.mark.unit
def test_lane_reports_are_classed_against_the_ledger_with_a_positive_control(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    reports = ctx.place("reports_dir")
    (reports / "fresh-lane.md").write_text("r")
    (reports / "closed-lane.md").write_text("r")
    (reports / "work.log").write_text("x")
    old = reports / "ancient-lane.md"
    old.write_text("r")
    week = time.time() - 9 * 86400
    os.utime(old, (week, week))
    ctx.ledger_path.write_text(
        "2026-10-22T00:00:00Z | TERMINAL | lane=closed-lane | friction=none | done\n"
    )
    out = _run("lane_reports_index", ctx)
    assert [r["lane"] for r in out["unclosed"]] == ["fresh-lane"]
    assert [r["lane"] for r in out["orphaned"]] == ["ancient-lane"]
    assert out["non_markdown_excluded"] == 1
    assert out["positive_control"]["lane"] == "closed-lane"


@pytest.mark.unit
def test_readme_and_venv_verdict_reads(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    section = _run("morning_readme", ctx, heading="Dropped work")
    assert "body two" in section["text"]
    assert "goal body" not in section["text"]
    assert "## Goal" in _run("morning_readme", ctx)["headings"]
    verdict = _run("venv_reconcile_verdict", ctx)
    assert verdict["verdict"] == "in sync"
    assert verdict["reconciler_exit"] == 0
    assert verdict["stale"] is False


@pytest.mark.unit
def test_claim_delegation_and_terminal_go_through_the_overlay_programs(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    claim = _run(
        "ledger_append_claim",
        ctx,
        est_hours=1,
        displaces="nothing queued",
        what="the dropped-work report for the day",
    )
    assert claim["lane"] == "dropped-work"
    with pytest.raises(MorningToolRefusalError, match="already claimed"):
        _run(
            "ledger_append_claim",
            ctx,
            est_hours=1,
            displaces="x",
            what="a second claim row",
        )
    draft = _run("delegate_prose", ctx, step="section-1", prompt="facts: n=3. " * 5)
    assert draft["ok"]
    assert draft["draft"] == "drafted prose"
    assert draft["run"] == "run-1"
    with pytest.raises(MorningToolRefusalError, match="not a reason"):
        _run(
            "delegate_dispose",
            ctx,
            draft_id="1",
            disposition="used",
            reason_code="wrong_answer",
        )
    with pytest.raises(MorningToolRefusalError, match="landed_in"):
        _run(
            "delegate_dispose",
            ctx,
            draft_id="1",
            disposition="used",
            reason_code="correct_as_is",
        )
    disposed = _run(
        "delegate_dispose",
        ctx,
        draft_id="1",
        disposition="used",
        reason_code="correct_as_is",
        landed_in="dropped_work",
    )
    assert (
        "--artifact doc:beta/tracking/2026-10-22-dropped-work.md" in disposed["stdout"]
    )
    cell = _run("delegation_cells", ctx)["cell"]
    assert cell == {"delegated": 1, "runs": ["run-1"], "reason": ""}
    _run(
        "ledger_append_terminal",
        ctx,
        outcome="done",
        summary="report delivered, 5 sections",
    )
    rows = (tmp_path / "appended.txt").read_text().splitlines()
    assert "| CLAIM | lane=dropped-work | ticket=OMN-18954 |" in rows[0]
    assert "est ~1 lane-hours; displaces nothing queued; (OMN-18954)" in rows[0]
    terminal = rows[-1]
    assert f"closes-CLAIM={claim['claimed']}" in terminal
    assert "delegated=1 runs=run-1" in terminal
    assert "date=2026-10-22" in terminal


@pytest.mark.unit
def test_a_refused_terminal_names_the_ledgers_reason_and_writes_its_friction_once(
    tmp_path: Path,
) -> None:
    """The 2026-10-22 proof: the ledger refused a TERMINAL on its count lint three times and the
    agent saw only the tail of the echoed row, never the reason (OMN-15897, OMN-17427)."""
    ctx = _ctx(tmp_path)
    _run(
        "ledger_append_claim",
        ctx,
        est_hours=1,
        displaces="nothing queued",
        what="the dropped-work report for the day",
    )
    appended = tmp_path / "appended.txt"
    refuse = _program(
        tmp_path / "refuse.py",
        "row = sys.argv[1]\n"
        "if '| TERMINAL |' in row:\n"
        "    sys.stderr.write('ledger_lock: OMN-15897 quantitative-claim lint rejected append '"
        "'-- line 1 (bare quantitative claim(s) with no adjacent citation: \\'262 PRs\\'): ' "
        "+ row + ' ' + 'x' * 900)\n"
        "    sys.exit(65)\n"
        f"open({str(appended)!r}, 'a').write(row + '\\n')",
    )
    refusing = ctx.model_copy(
        update={
            "commands": {
                **ctx.commands,
                "ledger_append_command": [sys.executable, str(refuse)],
            }
        }
    )
    for _ in range(2):
        with pytest.raises(MorningToolRefusalError) as refused:
            _run(
                "ledger_append_terminal",
                refusing,
                outcome="done",
                summary="report delivered; census of 262 PRs",
                friction="the census read twice",
            )
        assert "exit 65" in str(refused.value)
        assert "OMN-15897 quantitative-claim lint" in str(refused.value)
        assert "'262 PRs'" in str(refused.value)
        assert "FIX: put each count next to the citation" in str(refused.value)
    frictions = [
        row for row in appended.read_text().splitlines() if "| FRICTION |" in row
    ]
    assert len(frictions) == 1
    assert "the census read twice" in frictions[0]


@pytest.mark.unit
def test_a_timed_out_program_takes_its_children_with_it(tmp_path: Path) -> None:
    """The 2026-10-22 08:40Z fire: uv run's child outlived the tool's timeout (OMN-17427)."""
    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.base import _proc

    pid_file = tmp_path / "child.pid"
    parent = _program(
        tmp_path / "parent.py",
        "import subprocess, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)",
    )
    done = _proc([sys.executable, str(parent)], cwd=tmp_path, timeout=2)
    assert done["exit"] is None
    assert done["stderr"] == "timed out after 2 s"
    child = int(pid_file.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the timed-out program's child is still running")


@pytest.mark.unit
def test_a_commit_past_its_hook_time_leaves_no_index_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-22: a decisions-register commit outran 120 s in its hooks, and the index lock the
    killed git left refused every later commit in the run (OMN-17427)."""
    import subprocess

    from omnibase_core.validators.no_unguarded_git_subprocess import (
        scrub_git_location_env,
    )

    from omnimarket.nodes.node_morning_ground_state_orchestrator.tools import base

    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    git("config", "core.hooksPath", str(repo / ".hooks"))
    (repo / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-qm", "a")
    hooks = repo / ".hooks"
    hooks.mkdir()
    # Recent git holds the index lock while pre-commit runs; older git (2.43) does not, so
    # the hook holds it too and the case does not depend on the runner's git version.
    (hooks / "pre-commit").write_text("#!/bin/sh\ntouch .git/index.lock\nsleep 30\n")
    (hooks / "pre-commit").chmod(0o755)
    (repo / "a.txt").write_text("b\n")
    git("add", "a.txt")
    monkeypatch.setattr(base, "GIT_HOOK_TIMEOUT_S", 2)
    done = base._git_hooked(repo, "commit", "-m", "b")
    assert done["exit"] is None
    lock = repo / ".git" / "index.lock"
    assert done["stale_lock_removed"] == str(lock.resolve())
    assert not lock.exists()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    assert base._git_hooked(repo, "commit", "-qm", "b")["exit"] == 0


@pytest.mark.unit
def test_a_phase_session_waits_long_enough_for_its_tool_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The morning MCP server's 30 s startup default timed out under load (2026-10-22)."""
    from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers import (
        handler_morning_phase_tools as gateway,
    )

    monkeypatch.delenv("MCP_TIMEOUT", raising=False)
    assert gateway.session_env()["MCP_TIMEOUT"] == "180000"
    monkeypatch.setenv("MCP_TIMEOUT", "240000")
    assert gateway.session_env()["MCP_TIMEOUT"] == "240000"


@pytest.mark.unit
def test_a_terminal_without_a_claim_refuses(tmp_path: Path) -> None:
    with pytest.raises(MorningToolRefusalError, match="ledger_append_claim first"):
        _run(
            "ledger_append_terminal",
            _ctx(tmp_path),
            outcome="done",
            summary="nothing claimed yet",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("kb_push_branch", {}),
        ("kb_open_pr", {"body": "b" * 100}),
        ("pr_handoff", {}),
        ("linear_comment", {"ticket": "OMN-1", "body": "evidence " * 10}),
        (
            "linear_issue_set_state",
            {
                "ticket": "OMN-1",
                "to_state_type": "completed",
                "evidence": "receipt " * 10,
            },
        ),
        ("linear_issue_create", {"title": "a new ticket", "description": "desc " * 10}),
    ],
)
def test_every_publishing_or_linear_write_refuses_in_a_dry_run(
    name: str, args: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(MorningToolRefusalError, match="DRY run"):
        _run(name, _ctx(tmp_path, "session-goal", publish=False), **args)


@pytest.mark.unit
def test_an_artifact_write_is_owned_and_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, "session-goal")
    monkeypatch.setattr(effects, "_worktree_ready", lambda _ctx: None)
    with pytest.raises(MorningToolRefusalError, match="does not own"):
        _run("kb_write_artifact", ctx, artifact="dropped_work", content="x")
    with pytest.raises(MorningToolRefusalError, match="caps it at 89"):
        _run("kb_write_artifact", ctx, artifact="goal", content="line\n" * 90)
    out = _run("kb_write_artifact", ctx, artifact="goal", content="state_as_of: x\n")
    assert out["lines"] == 1
    assert (ctx.kb_worktree / "beta/GOAL.md").read_text() == "state_as_of: x\n"


@pytest.mark.unit
def test_linear_sprint_is_the_dated_project_covering_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = [
        {
            "id": "p1",
            "name": "Sprint A",
            "startDate": "2026-10-12",
            "targetDate": "2026-10-18",
            "completedAt": None,
        },
        {
            "id": "p2",
            "name": "Sprint B",
            "startDate": "2026-10-19",
            "targetDate": "2026-10-25",
            "completedAt": None,
        },
    ]

    def fake(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {"projects": {"nodes": projects, "pageInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(linear_tools, "_graphql", fake)
    assert linear_tools._sprint("2026-10-22")["id"] == "p2"
    assert linear_tools._sprint("2026-10-18")["id"] == "p1"


@pytest.mark.unit
def test_local_reads_name_no_free_path() -> None:
    for tool in (
        *local_reads.READ_TOOLS,
        *linear_tools.LINEAR_TOOLS,
        *effects.EFFECT_TOOLS,
    ):
        fields = set(tool.input_model.model_fields)
        assert not fields & {"path", "file", "command", "argv", "url", "cwd"}


@pytest.mark.unit
def test_the_verifier_runs_only_on_a_closable_ticket_and_within_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, "morning-triage")
    receipt = {
        "status": "success",
        "result": {
            "terminal_payload": {"status": "verified", "total_checks": 2, "checks": []}
        },
    }
    verifier = _program(
        tmp_path / "dod.py", f"print({json.dumps(json.dumps(receipt))})"
    )
    ctx = ctx.model_copy(
        update={
            "commands": {
                **ctx.commands,
                "dod_verify_command": [sys.executable, str(verifier)],
            }
        }
    )
    with pytest.raises(MorningToolRefusalError, match="open PR omnidemo#9"):
        _run("dod_verify", ctx, ticket="OMN-1234")
    with pytest.raises(MorningToolRefusalError, match="no merged PR"):
        _run("dod_verify", ctx, ticket="OMN-4242")
    state = cast(dict[str, Any], json.loads(ctx.place("pr_watcher_state").read_text()))
    state["merges"]["omnidemo#11"] = {"title": "fix(OMN-777): done", "merged_at": "x"}
    ctx.place("pr_watcher_state").write_text(json.dumps(state))
    out = _run("dod_verify", ctx, ticket="OMN-777")
    assert out["status"] == "verified"
    assert out["total_checks"] == 2
    monkeypatch.setattr(effects, "DOD_VERIFY_BUDGET", 1)
    with pytest.raises(MorningToolRefusalError, match="budget"):
        _run("dod_verify", ctx, ticket="OMN-777")


@pytest.mark.unit
def test_the_morning_pr_is_opened_once_and_handed_off_with_the_receipts_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path, "session-goal", publish=True)
    ctx.kb_worktree.mkdir(parents=True)
    calls = tmp_path / "calls.txt"
    create = _program(
        tmp_path / "gh.py",
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print('https://github.com/OmniNode-ai/kb_demo/pull/77')",
    )
    handoff = _program(
        tmp_path / "handoff.py",
        f"open({str(calls)!r}, 'a').write('HANDOFF ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('OK handed off')",
    )
    ctx = ctx.model_copy(
        update={
            "kb_clone": tmp_path / "kb_demo",
            "commands": {
                **ctx.commands,
                "kb_pr_create_command": [sys.executable, str(create)],
                "pr_handoff_command": [sys.executable, str(handoff)],
            },
        }
    )
    monkeypatch.setattr(effects, "_worktree_ready", lambda _ctx: None)
    monkeypatch.setattr(effects, "_git_out", lambda *_args: "f" * 40)
    _run(
        "ledger_append_claim",
        ctx,
        est_hours=0.5,
        displaces="nothing queued",
        what="the goal file and the morning PR",
    )
    opened = _run("kb_open_pr", ctx, body="what each phase delivered " * 5)
    assert opened["number"] == 77
    assert opened["title"] == "docs(OMN-1): morning ground state 2026-10-22"
    out = _run("pr_handoff", ctx)
    assert out["exit"] == 0
    lines = calls.read_text().splitlines()
    assert "--base main --head automation/demo-2026-10-22" in lines[0]
    handed = lines[1]
    assert handed.startswith("HANDOFF kb_demo#77 session-goal --needs land")
    assert "--delegation delegated=1 runs=run-1" in handed
    assert "--keep-worktree" in handed
