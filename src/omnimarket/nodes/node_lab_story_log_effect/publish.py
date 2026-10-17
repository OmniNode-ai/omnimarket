# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish a written story log as a pull request on the target repository.

A branch is cut from the clone's current default-branch ref into its own linked worktree, the log
is committed and pushed, and the pull request is opened through ``onex scm pr-create``. Nothing in
the canonical clone is checked out, and a log the default branch already carries byte for byte is
not published again.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.node_lab_story_log_effect.models import STORY_LOG_RELATIVE_DIR

PR_LANE = "lab-story-log"
TICKET = "OMN-17427"

Runner = Callable[[list[str], Path | None], "subprocess.CompletedProcess[str]"]


class PublishError(RuntimeError):
    """A git or pull-request step failed; the message names the command and its stderr."""


@dataclass(frozen=True)
class PublishResult:
    published: bool
    branch: str
    worktree: Path
    detail: str


def _run(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _checked(run: Runner, args: list[str], cwd: Path | None = None) -> str:
    done = run(args, cwd)
    if done.returncode:
        raise PublishError(
            f"{' '.join(args[:4])} failed ({done.returncode}): {done.stderr.strip()}"
        )
    return done.stdout.strip()


def publish_story_log(
    *,
    clone: Path,
    repository: str,
    worktree_root: Path,
    label: str,
    markdown: str,
    summary: str,
    run: Runner = _run,
) -> PublishResult:
    base_ref = _checked(
        run,
        [
            "git",
            "-C",
            str(clone),
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
    )
    base_branch = base_ref.removeprefix("origin/")
    relative = f"{STORY_LOG_RELATIVE_DIR}/{label}.md"
    branch = f"omn-17427-lab-story-log-{label.lower()}"
    worktree = worktree_root / f"lab-story-log-{label.lower()}" / "checkout"
    existing = run(["git", "-C", str(clone), "show", f"{base_ref}:{relative}"], None)
    if existing.returncode == 0 and existing.stdout == markdown:
        return PublishResult(
            False, branch, worktree, f"{relative} is already on {base_ref}"
        )
    sha = _checked(run, ["git", "-C", str(clone), "rev-parse", base_ref])
    _checked(
        run,
        [
            "git",
            "-C",
            str(clone),
            "worktree",
            "add",
            str(worktree),
            "-b",
            branch,
            sha,
        ],
    )
    target = worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    title = f"docs({TICKET}): lab story log {label}"
    _checked(run, ["git", "-C", str(worktree), "add", relative])
    _checked(run, ["git", "-C", str(worktree), "commit", "-m", f"{title}\n\n{summary}"])
    _checked(run, ["git", "-C", str(worktree), "push", "-u", "origin", branch])
    body = worktree_root / f"lab-story-log-{label.lower()}" / "PR_BODY.md"
    body.write_text(f"{summary}\n\nCloses {TICKET}\n", encoding="utf-8")
    _checked(
        run,
        [
            "onex",
            "scm",
            "pr-create",
            repository,
            "--head",
            branch,
            "--base",
            base_branch,
            "--bus-lane",
            "dev",
            "--lane",
            PR_LANE,
            "--title",
            title,
            "--body-file",
            str(body),
        ],
    )
    return PublishResult(
        True, branch, worktree, f"opened a pull request for {relative}"
    )


__all__ = [
    "PublishError",
    "PublishResult",
    "Runner",
    "publish_story_log",
]
