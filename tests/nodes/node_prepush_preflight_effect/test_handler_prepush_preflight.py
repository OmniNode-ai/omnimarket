# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-push evaluation of a lane's pending push range (OMN-22452).

Two lanes on 2026-11-04 passed the commit-stage preflight and were refused at push by
``check-node-boundary-imports`` and ``ticket-contract-prerequisite``. The parser cases replay a pre-commit
transcript carrying both refusals; the process cases run the real handler and real ``pre-commit`` on a scratch
repository with ``language: system`` pre-push hooks (no network).
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_prepush_preflight_effect.handlers.handler_prepush_preflight import (
    HandlerPrepushPreflight,
    parse_refusals,
)
from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_preflight_request import (
    ModelPrepushPreflightRequest,
)

# pre-commit's transcript of the two refusals, in the order the repository's hooks run: the hook ids and the finding
# lines are the ones the hooks print (node_boundary_import_check's baseline-growth refusal, the ticket-contract
# prerequisite's missing-contract failure).
RECORDED_REFUSALS = """\
Ruff format..............................................................Passed
Check node boundary imports..............................................Failed
- hook id: check-node-boundary-imports
- duration: 4.12s
- exit code: 1

Refusing baseline growth; new edges:
  omnibase_infra.nodes.node_a.handlers -> omnibase_infra.nodes.node_b.models
Ticket contract at pushed head (OMN-20864)...............................Failed
- hook id: ticket-contract-prerequisite
- duration: 0.31s
- exit code: 1

pull request cites OMN-22446 but carries no contracts/OMN-22446.yaml
Generated event chains hold for every walker path........................Skipped
"""


def test_the_recorded_refusals_are_parsed_with_hook_id_and_finding() -> None:
    refusals = parse_refusals(RECORDED_REFUSALS)
    assert [r.hook_id for r in refusals] == [
        "check-node-boundary-imports",
        "ticket-contract-prerequisite",
    ]
    assert refusals[0].exit_code == 1
    assert "Refusing baseline growth; new edges:" in refusals[0].finding
    assert (
        "node_a.handlers -> omnibase_infra.nodes.node_b.models" in refusals[0].finding
    )
    assert (
        refusals[1].finding
        == "pull request cites OMN-22446 but carries no contracts/OMN-22446.yaml"
    )


def test_a_transcript_of_passed_and_skipped_hooks_has_no_refusal() -> None:
    assert (
        parse_refusals("Lint....Passed\nTypes.....(no files to check)Skipped\n") == ()
    )


def test_the_finding_is_capped() -> None:
    body = "".join(f"line {i}\n" for i in range(50))
    out = parse_refusals(
        f"Hook....Failed\n- hook id: h\n- exit code: 1\n\n{body}", finding_lines=5
    )
    assert out[0].finding.splitlines() == [f"line {i}" for i in range(5)]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def make_repo(tmp_path: Path, hooks: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "lane@example.invalid")
    git(repo, "config", "user.name", "lane")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n" + textwrap.indent(hooks, "      ")
    )
    (repo / "README").write_text("seed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "checkout", "-q", "-b", "lane")
    return repo


def pre_push_hook(hook_id: str, command: str, stage: str = "pre-push") -> str:
    return (
        f'- id: {hook_id}\n  name: {hook_id}\n  language: system\n  entry: "{command}"\n'
        f"  pass_filenames: false\n  always_run: true\n  stages: [{stage}]\n"
    )


def commit_change(repo: Path) -> None:
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "change")


def evaluate(repo: Path):
    return HandlerPrepushPreflight().handle(
        ModelPrepushPreflightRequest(worktree=str(repo), base_ref="main")
    )


def test_a_refusing_pre_push_hook_is_reported_with_its_id_and_finding(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path, pre_push_hook("boundary", "sh -c 'echo new edge a-to-b >&2; exit 1'")
    )
    commit_change(repo)
    result = evaluate(repo)
    assert result.status == "refused"
    assert [(r.hook_id, r.exit_code) for r in result.refusals] == [("boundary", 1)]
    assert "new edge a-to-b" in result.refusals[0].finding
    assert result.range_from == git(repo, "rev-parse", "main")
    assert result.range_to == git(repo, "rev-parse", "HEAD")


def test_a_passing_pre_push_hook_is_ok(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, pre_push_hook("fine", "true"))
    commit_change(repo)
    assert evaluate(repo).status == "ok"


def test_a_commit_stage_hook_is_not_run_by_the_pre_push_stage(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path, pre_push_hook("commit-only", "false", stage="pre-commit")
    )
    commit_change(repo)
    assert evaluate(repo).status == "ok"


def test_the_staged_change_is_part_of_the_range_before_it_is_committed(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path,
        pre_push_hook(
            "sees-a-py",
            "sh -c 'git diff --name-only $PRE_COMMIT_FROM_REF $PRE_COMMIT_TO_REF | grep -q a.py'",
        ),
    )
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    head_before = git(repo, "rev-parse", "HEAD")
    result = evaluate(repo)
    assert result.status == "ok", result
    assert (
        git(repo, "rev-parse", "HEAD") == head_before
    )  # nothing was committed, no ref moved
    assert result.range_to != head_before


def test_nothing_pending_is_an_empty_range(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, pre_push_hook("never", "false"))
    assert evaluate(repo).status == "empty-range"


def test_no_config_and_no_base_are_named(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, pre_push_hook("never", "false"))
    commit_change(repo)
    unresolved = HandlerPrepushPreflight().handle(
        ModelPrepushPreflightRequest(worktree=str(repo), base_ref="origin/none")
    )
    assert unresolved.status == "no-range"
    (repo / ".pre-commit-config.yaml").unlink()
    assert evaluate(repo).status == "no-config"


def test_a_skip_variable_in_the_environment_does_not_skip_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path, pre_push_hook("boundary", "false"))
    commit_change(repo)
    monkeypatch.setenv("SKIP", "boundary")
    assert evaluate(repo).status == "refused"


def test_a_hook_past_the_timeout_is_an_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, pre_push_hook("slow", "sleep 30"))
    commit_change(repo)
    result = HandlerPrepushPreflight().handle(
        ModelPrepushPreflightRequest(worktree=str(repo), base_ref="main", timeout_s=2)
    )
    assert result.status == "error"
    assert "timeout" in result.error_tail


def test_the_report_carries_the_failing_keys_a_lane_preflight_takes(
    tmp_path: Path,
) -> None:
    repo = make_repo(
        tmp_path, pre_push_hook("boundary", "sh -c 'echo new edge a-to-b >&2; exit 1'")
    )
    commit_change(repo)
    report = evaluate(repo).report
    assert report["outcome"] == "failing"
    assert report["hook"] == "boundary"
    assert report["failing"] == ["boundary"]
    assert report["error_tail"].startswith("pre-push boundary (exit 1): ")
    assert "new edge a-to-b" in report["error_tail"]
    assert report["prepush"]["status"] == "refused"


def test_the_report_of_a_clean_range_names_no_outcome(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, pre_push_hook("fine", "true"))
    commit_change(repo)
    assert set(evaluate(repo).report) == {"prepush"}


def test_the_landing_branch_is_origin_head_when_the_request_names_no_base(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path, pre_push_hook("boundary", "false"))
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    commit_change(repo)
    result = HandlerPrepushPreflight().handle(
        ModelPrepushPreflightRequest(worktree=str(repo))
    )
    assert result.status == "refused"
    assert result.range_from == git(repo, "rev-parse", "main")
