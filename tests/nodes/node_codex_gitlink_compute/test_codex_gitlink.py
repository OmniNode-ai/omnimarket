# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generated gitlink lifecycle preserves worktrees and operator-owned locks."""

import os
import shlex
import subprocess

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_codex_gitlink_compute import NodeCodexGitlinkCompute
from omnimarket.nodes.node_codex_gitlink_compute.models.model_codex_gitlink import (
    ModelCodexGitlinkRequest,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def worktree(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=scrub_git_location_env(env),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    clone = tmp_path / "clone"
    tree = tmp_path / "tree"
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    git("init", "-q", "-b", "main", str(clone))
    git(
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "init",
        cwd=clone,
    )
    git("worktree", "add", "-q", "--detach", str(tree), "main", cwd=clone)
    fragments = NodeCodexGitlinkCompute().handle(
        ModelCodexGitlinkRequest(
            "fixture-lock", "saved-gitlink", (".agents", ".aws", ".codex")
        )
    )
    preamble = f"W={shlex.quote(str(tree))}; C={shlex.quote(str(ctx))}\n"
    return clone, tree, ctx, env, git, fragments, preamble


def test_gitlink_aside_survives_prune_and_restores(worktree):
    clone, tree, ctx, env, _git, fragments, preamble = worktree
    script = "\n".join(
        [
            preamble,
            'GD=$(git -C "$W" rev-parse --path-format=absolute --git-dir)',
            fragments.aside_line,
            '[ ! -e "$W/.git" ] && echo LINK-ASIDE',
            f"env -u GIT_DIR -u GIT_WORK_TREE git -C {shlex.quote(str(clone))} worktree prune",
            '[ -d "$GD" ] && echo KEPT',
            fragments.restore_line,
            "unset GIT_DIR GIT_WORK_TREE",
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env=scrub_git_location_env(env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.split() == ["LINK-ASIDE", "KEPT"]
    assert (tree / ".git").is_file()
    assert not (ctx / "saved-gitlink").exists()
    assert not list((clone / ".git/worktrees").glob("*/locked"))


def test_restore_keeps_a_lock_it_did_not_write(worktree):
    clone, tree, _, env, git, fragments, preamble = worktree
    git("worktree", "lock", "--reason", "operator-owned", str(tree), cwd=clone)
    subprocess.run(
        ["bash", "-c", preamble + fragments.restore_line],
        env=scrub_git_location_env(env),
        capture_output=True,
        check=False,
    )
    locks = list((clone / ".git/worktrees").glob("*/locked"))
    assert len(locks) == 1
    assert locks[0].read_text().strip() == "operator-owned"


def test_put_back_repairs_only_this_trees_core_worktree(worktree):
    clone, tree, _, env, git, fragments, preamble = worktree
    git("config", "core.worktree", str(tree), cwd=clone)
    subprocess.run(
        ["bash", "-c", preamble + fragments.put_back_line],
        env=scrub_git_location_env(env),
        check=True,
    )
    proc = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(clone / ".git/config"),
            "--get",
            "core.worktree",
        ],
        env=scrub_git_location_env(env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    other = tree.parent / "other"
    other.mkdir()
    git("config", "core.worktree", str(other), cwd=clone)
    subprocess.run(
        ["bash", "-c", preamble + fragments.put_back_line],
        env=scrub_git_location_env(env),
        check=True,
    )
    assert git(
        "config", "--file", str(clone / ".git/config"), "--get", "core.worktree"
    ) == str(other)
