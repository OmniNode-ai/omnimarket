# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem and git safety checks for the remote lane retention handler.

This stdlib source is also inlined into the host payload, where Market need not
be installed. Both trees are inspected before either can be removed.
"""

import os
import subprocess
from pathlib import Path


def _inside(root: Path, path: Path) -> Path:
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact outside lab root") from exc
    if ".." in relative.parts:
        raise ValueError("parent traversal")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("symlink in retention path")
    return relative


def _git(tree: Path, *args: str) -> str:
    # A remote engine may export its own GIT_DIR/WORK_TREE/INDEX_FILE. Read the
    # candidate repository, never that engine's checkout; do not refresh its index.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _git_keep_reason(tree: Path, trees: list[Path]) -> str:
    """The runner's clean-status / remote-reachability test, without a WIP push."""
    try:
        # A clone owns the git directories of every registered linked worktree,
        # even when that other tree is clean or the registration looks stale.
        common = Path(
            _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        owns_common = any(common.is_relative_to(candidate) for candidate in trees)
        worktrees = common / "worktrees"
        if owns_common and (
            worktrees.is_symlink() or (worktrees.exists() and any(worktrees.iterdir()))
        ):
            return "holds-worktree-git-dir"
        if Path(_git(tree, "rev-parse", "--show-toplevel")).resolve() != tree.resolve():
            return "worktree-path-mismatch"
        if _git(
            tree,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ):
            return "uncommitted-changes"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "status-unreadable"
    try:
        # Include other local refs when their object store would be deleted. For
        # an external store only this worktree's HEAD is at risk. A branch need
        # not track an upstream if its commits are on another remote.
        refs = ["--all"] if owns_common else []
        if (
            _git(tree, "rev-list", "--count", "HEAD", *refs, "--not", "--remotes")
            != "0"
        ):
            return "unpushed-commits"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "remotes-unreadable"
    return ""


def _trees_keep_reason(root: Path, trees: list[Path]) -> str:
    """Check both trees before removing either; inspect nested repositories too."""
    try:
        for tree in trees:
            _inside(root, tree)
            if not tree.exists():
                continue

            # Do not follow links or walk object stores. os.walk's default ignores
            # scan errors, which would make an unreadable nested repository unsafe.
            def fail(error: OSError) -> None:
                raise error

            for directory, names, files in os.walk(tree, onerror=fail):
                path = Path(directory)
                if ".git" in names or ".git" in files:
                    _inside(root, path / ".git")
                    reason = _git_keep_reason(path, trees)
                    if reason:
                        return f"{reason}: {path.relative_to(root / 'runs')}"
                    if ".git" in names:
                        names.remove(".git")
    except (OSError, ValueError) as exc:
        return f"tree-unreadable: {exc}"
    return ""
