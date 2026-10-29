# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21496 T2.6: the consumed omnibase_core commit hooks refuse and write nothing.

The commit hooks of this repository are the check-only ids that omnibase_core
publishes (``commit-hygiene``, ``commit-hygiene-message``). This canary runs
them through ``pre-commit`` on a throwaway tree, with the repository pin the
committed ``.pre-commit-config.yaml`` carries for them, and checks that a file
without a final newline, a staged ``tmp/`` file and a commit message with an AI
co-author trailer are each refused and that every byte of the tree is the same
after each run. What a hook id means is core's rule, not asserted here.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

PRE_COMMIT_STAGE_ID = "commit-hygiene"
COMMIT_MSG_STAGE_ID = "commit-hygiene-message"


def _consumed_core_block() -> dict[str, Any]:
    """The committed repo block that consumes the core commit-hygiene ids."""
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    blocks = [
        repo
        for repo in config["repos"]
        if {PRE_COMMIT_STAGE_ID, COMMIT_MSG_STAGE_ID}
        <= {hook["id"] for hook in repo["hooks"]}
    ]
    assert len(blocks) == 1, "exactly one repo block consumes the commit-hygiene ids"
    return {"repo": blocks[0]["repo"], "rev": blocks[0]["rev"]}


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_consumed_commit_hooks_refuse_and_leave_the_tree_byte_identical(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(tree)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    config = tmp_path / "config.yaml"
    block = _consumed_core_block()
    block["hooks"] = [{"id": PRE_COMMIT_STAGE_ID}, {"id": COMMIT_MSG_STAGE_ID}]
    config.write_text(yaml.safe_dump({"repos": [block]}))

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pre_commit", "run", *args, "--config", str(config)],
            capture_output=True,
            text=True,
            cwd=tree,
            env=scrub_git_location_env(os.environ),
            check=False,
        )

    def stage(name: str, content: bytes) -> None:
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        subprocess.run(
            ["git", "add", name],
            cwd=tree,
            check=True,
            env=scrub_git_location_env(os.environ),
        )

    def unstage(name: str) -> None:
        subprocess.run(
            ["git", "rm", "-q", "--cached", name],
            cwd=tree,
            check=True,
            env=scrub_git_location_env(os.environ),
        )

    # positive control: a clean staged file passes, so a refusal below is the rule's
    stage("clean.txt", b"one line\n")
    result = run(PRE_COMMIT_STAGE_ID)
    assert result.returncode == 0, result.stdout + result.stderr
    unstage("clean.txt")

    for name, content, rule in [
        ("notes.txt", b"no final newline", "missing_final_newline"),
        ("tmp/scratch.txt", b"scratch\n", "staged_tmp_file"),
    ]:
        stage(name, content)
        before = _tree_digest(tree)
        result = run(PRE_COMMIT_STAGE_ID)
        assert result.returncode != 0, result.stdout + result.stderr
        assert rule in result.stdout + result.stderr
        assert _tree_digest(tree) == before
        unstage(name)

    message = tree / "COMMIT_EDITMSG"
    message.write_bytes(
        b"fix: a change\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    )
    before = _tree_digest(tree)
    result = run(
        COMMIT_MSG_STAGE_ID,
        "--hook-stage",
        "commit-msg",
        "--commit-msg-filename",
        "COMMIT_EDITMSG",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "ai_coauthor_trailer" in result.stdout + result.stderr
    assert _tree_digest(tree) == before
