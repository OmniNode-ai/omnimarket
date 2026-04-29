# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_pr_polish live execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.unit
def test_live_cli_resolves_worktree_and_writes_result_json(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "state" / "pr-polish" / "run-1"

    _write_executable(
        fake_bin / "gh",
        """#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  if [[ "$7" == "headRefName" ]]; then
    printf '{"headRefName":"feature/test-pr"}'
    exit 0
  fi
  if [[ "$7" == "headRefOid" ]]; then
    printf '{"headRefOid":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"}'
    exit 0
  fi
  if [[ "$7" == "id" ]]; then
    printf '{"id":"PR_node_123"}'
    exit 0
  fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  if echo "$*" | grep -q 'enablePullRequestAutoMerge'; then
    printf '%s\\n' "$*" > "${ONEX_STATE_DIR}/gh-graphql-argv.txt"
    exit 0
  fi
  printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}'
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  printf 'worktree {worktree}\\nbranch refs/heads/feature/test-pr\\n\\n'
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "rev-parse" ]]; then
  if [[ "$4" == "--abbrev-ref" ]]; then
    printf 'feature/test-pr\\n'
    exit 0
  fi
  printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\\n'
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "diff" ]]; then
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "checkout" ]]; then
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "push" ]]; then
  printf '%s\\n' "$*" > "${{ONEX_STATE_DIR}}/git-push-argv.txt"
  exit 0
fi
echo "unexpected git invocation: $*" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
if [[ "$1" == "run" && "$2" == "pre-commit" ]]; then
  printf '%s\\n' "$*" > "${ONEX_STATE_DIR}/pre-commit-argv.txt"
  exit 0
fi
echo "unexpected uv invocation: $*" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "pre-commit",
        """#!/usr/bin/env bash
if [[ "$1" == "install" ]]; then
  exit 0
fi
echo "unexpected pre-commit invocation: $*" >&2
exit 1
""",
    )
    env = {
        **os.environ,
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_polish",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--pr-number",
            "42",
            "--run-dir",
            str(run_dir),
            "--required-clean-runs",
            "2",
            "--max-iterations",
            "3",
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["final_phase"] == "done"

    result_path = run_dir / "result.json"
    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["final_state"] == "COMPLETE"
    assert result["repo"] == "OmniNode-ai/omnimarket"
    assert result["pr_number"] == 42
    assert result["worktree_path"] == str(worktree)
    assert result["push_status"] == "pushed"
    assert result["auto_merge_status"] == "armed"
    assert result["coderabbit_triage"]["total_threads"] == 0
    assert result["phase_results"] == [
        {"phase": "resolve_conflicts", "status": "clean"},
        {
            "phase": "fix_ci",
            "status": "deferred_to_required_checks",
            "detail": "CI state is enforced by GitHub required checks before merge.",
        },
        {
            "phase": "address_comments",
            "status": "handled_by_market_coderabbit_triage",
        },
        {
            "phase": "local_review",
            "status": "deferred_to_pre_push_gate",
            "detail": "Push mode runs the full pre-commit gate once before publishing.",
        },
    ]
    assert not (tmp_path / "state" / "claude-argv.txt").exists()
    assert (
        "run pre-commit run --all-files"
        in (tmp_path / "state" / "pre-commit-argv.txt").read_text()
    )
    assert (
        "push origin HEAD:feature/test-pr"
        in (tmp_path / "state" / "git-push-argv.txt").read_text()
    )
    assert (
        "enablePullRequestAutoMerge"
        in (tmp_path / "state" / "gh-graphql-argv.txt").read_text()
    )


@pytest.mark.unit
def test_live_cli_no_push_skips_finalize_side_effects(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "state" / "pr-polish" / "run-2"

    _write_executable(
        fake_bin / "gh",
        """#!/usr/bin/env bash
if [[ "$1" == "pr" && "$2" == "view" ]]; then
  printf '{"headRefName":"feature/no-push"}'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}'
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
if [[ "$1" == "worktree" && "$2" == "list" ]]; then
  printf 'worktree {worktree}\\nbranch refs/heads/feature/no-push\\n\\n'
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "rev-parse" ]]; then
  printf 'feature/no-push\\n'
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "diff" ]]; then
  exit 0
fi
if [[ "$1" == "-C" && "$3" == "checkout" ]]; then
  exit 0
fi
echo "unexpected git invocation: $*" >&2
exit 1
""",
    )
    _write_executable(
        fake_bin / "pre-commit",
        """#!/usr/bin/env bash
if [[ "$1" == "install" ]]; then
  exit 0
fi
echo "unexpected pre-commit invocation: $*" >&2
exit 1
""",
    )
    env = {
        **os.environ,
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_polish",
            "--repo",
            "OmniNode-ai/omnimarket",
            "--pr-number",
            "42",
            "--run-dir",
            str(run_dir),
            "--no-push",
            "--no-automerge",
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads((run_dir / "result.json").read_text())
    assert result["push_status"] == "skipped"
    assert result["auto_merge_status"] == "skipped"
    assert result["coderabbit_triage"]["dry_run"] is True
    assert {
        "phase": "local_review",
        "status": "skipped_non_mutating_mode",
        "detail": "Use push mode to run local review before publishing.",
    } in result["phase_results"]
    assert not (tmp_path / "state" / "claude-argv-no-push.txt").exists()
