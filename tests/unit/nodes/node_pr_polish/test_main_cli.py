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

_REPO_ROOT = Path(__file__).resolve().parents[5]


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
  printf '{"headRefName":"feature/test-pr"}'
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
  printf 'feature/test-pr\\n'
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
    _write_executable(
        fake_bin / "claude",
        """#!/usr/bin/env bash
printf '%s\\n' "$PWD" > "${ONEX_STATE_DIR}/claude-cwd.txt"
printf '%s\\n' "$*" > "${ONEX_STATE_DIR}/claude-argv.txt"
exit 0
""",
    )

    env = {
        **os.environ,
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_BIN": str(fake_bin / "claude"),
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
    assert (tmp_path / "state" / "claude-cwd.txt").read_text().strip() == str(worktree)
    assert "/onex:pr_polish 42" in (tmp_path / "state" / "claude-argv.txt").read_text()
