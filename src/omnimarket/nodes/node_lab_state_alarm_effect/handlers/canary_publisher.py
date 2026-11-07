# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publishes the floor's age and the alarm heartbeat where a hosted canary can read them (OMN-19998, 17a; plan W23).

The canary runs on a hosted runner outside the lab and cannot reach the tailnet, so the cycle publishes a small status
document (``ModelCanaryStatus``: three times and two bounds, nothing internal) to one branch of a repository the
overlay names. The branch holds a single commit that is replaced on every publish, so it does not grow. The remote, the
branch, the file name, the bounds and the commit identity are overlay data; this module names none of them.

Pure of the network except through the injected ``run_git``; a failed git step raises ``CanaryPublishError`` and the
caller records it, so a publisher that cannot publish is a typed failure and the canary then reads the old status as
silent once it is older than its bound.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.models.floor_17a import ModelCanaryPublishOverlay, ModelCanaryStatus


class CanaryPublishError(RuntimeError):
    """The status could not be published; the message is the typed reason, never a credential."""


RunGit = Callable[[Sequence[str], Path, int], tuple[int, str]]


def run_git(argv: Sequence[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"git did not run ({type(exc).__name__})"
    return done.returncode, (done.stderr or done.stdout)[-300:]


class GitCanaryPublisher:
    def __init__(
        self,
        overlay: ModelCanaryPublishOverlay,
        clone_dir: Path,
        git: RunGit = run_git,
    ) -> None:
        self._overlay = overlay
        self._dir = clone_dir
        self._git = git

    def _step(self, *argv: str) -> str:
        code, tail = self._git(argv, self._dir, self._overlay.timeout_s)
        if code != 0:
            raise CanaryPublishError(
                f"git {argv[0]} failed (exit {code}): {tail.strip()[:160]}"
            )
        return tail

    def publish(self, status: ModelCanaryStatus) -> None:
        ov = self._overlay
        self._dir.mkdir(parents=True, exist_ok=True)
        if not (self._dir / ".git").exists():
            self._step("init", "-q")
            self._step("remote", "add", "origin", ov.remote)
        (self._dir / ov.file_name).write_text(
            status.model_dump_json(indent=1) + "\n", encoding="utf-8"
        )
        self._step("add", "--", ov.file_name)
        identity = (
            "-c",
            f"user.name={ov.committer_name}",
            "-c",
            f"user.email={ov.committer_email}",
            "-c",
            "commit.gpgsign=false",
        )
        message = f"status {status.published_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        has_commit = (
            self._git(("rev-parse", "--verify", "-q", "HEAD"), self._dir, ov.timeout_s)[
                0
            ]
            == 0
        )
        if has_commit:
            self._step(*identity, "commit", "-q", "--amend", "-m", message)
        else:
            self._step("checkout", "-q", "-B", ov.branch)
            self._step(*identity, "commit", "-q", "-m", message)
        self._step("push", "-q", "--force", "origin", f"HEAD:refs/heads/{ov.branch}")


__all__ = ["CanaryPublishError", "GitCanaryPublisher", "RunGit", "run_git"]
