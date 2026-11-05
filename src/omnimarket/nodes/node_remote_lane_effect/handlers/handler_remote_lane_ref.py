# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sha a remote lane's worktree is built from (OMN-20669).

A port of the remote-lane runner's ``resolve_sha`` and ``resolve_landing_branch``.
Refs are read with ``git ls-remote`` through the repository's canonical clone when
this host holds one, else its public URL; never the hosting API, whose shared quota
a dispatch must not spend.

Every option goes before the remote. ``git ls-remote <remote> --symref HEAD`` reads
``--symref`` as a ref pattern, prints no ``ref:`` line and so no landing branch: the
runner's ``<clone> ls-remote origin --symref HEAD`` failed every dispatch without a
sha that way on 2026-10-09.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from omnimarket.config.service_endpoints import service_endpoint_url

from ..models import (
    SHA_RE,
    EnumRemoteLaneRefRefusal,
    ModelRemoteLaneRefRequest,
    ModelRemoteLaneRefResult,
)
from ..protocols import ProtocolRemoteLaneGit, RemoteLanePortError

SYMREF_RE = re.compile(r"^ref:\s*refs/heads/(\S+)\s+HEAD\s*$", re.MULTILINE)


class _ReadFailedError(Exception):
    pass


class HandlerRemoteLaneRef:
    """Resolve the sha a lane builds from: the named sha, ref, or the landing branch."""

    def __init__(self, git: ProtocolRemoteLaneGit | None = None) -> None:
        from ..protocols.local_remote_lane_adapters import LocalRemoteLaneGit

        self._git = git if git is not None else LocalRemoteLaneGit()

    def _remote(self, request: ModelRemoteLaneRefRequest) -> tuple[list[str], str]:
        """``git`` argv up to ``ls-remote``, and the remote that follows every option."""
        candidates: list[str] = []
        if request.repo == "omnibase_internal" and request.omnibase_internal_home:
            candidates.append(request.omnibase_internal_home)
        if request.omni_home:
            home = request.omni_home.rstrip("/")
            parent = home.rpartition("/")[0] or "/"
            candidates += [
                f"{home}/{request.repo}",
                f"{parent.rstrip('/')}/{request.repo}",
            ]
        for clone in candidates:
            if self._git.is_clone(clone):
                return ["git", "-C", clone, "ls-remote"], "origin"
        return (
            [
                "git",
                "ls-remote",
            ],
            f"{service_endpoint_url('github.web_url')}/{request.owner}/{request.repo}.git",
        )

    def _ls_remote(
        self,
        request: ModelRemoteLaneRefRequest,
        options: Sequence[str],
        patterns: Sequence[str],
    ) -> tuple[str, str]:
        head, remote = self._remote(request)
        try:
            done = self._git.run(
                [*head, *options, remote, *patterns], request.timeout_s
            )
        except RemoteLanePortError as exc:
            raise _ReadFailedError(f"ls-remote could not run: {exc}") from exc
        if done.returncode != 0:
            raise _ReadFailedError(
                f"ls-remote exited {done.returncode}: {done.stderr.strip()[:200]}"
            )
        return done.stdout, remote

    def handle(self, request: ModelRemoteLaneRefRequest) -> ModelRemoteLaneRefResult:
        """Check the default branch and the named branch before send (OMN-19998).

        A named sha passes unread. Otherwise the remote's default branch is read
        first, and then, in one read, the default branch and the named ref: a branch
        not yet pushed cannot be resolved, so it is refused as BRANCH_NOT_PUSHED with
        the default branch named, before any lane is sent from it.
        """
        if request.sha:
            if SHA_RE.match(request.sha):
                return ModelRemoteLaneRefResult(sha=request.sha)
            return ModelRemoteLaneRefResult(
                sha=None,
                error="the named sha is not a 40-character hex sha",
                refusal=EnumRemoteLaneRefRefusal.SHA_MALFORMED,
            )
        landing: str | None = None
        remote = ""
        try:
            out, remote = self._ls_remote(request, ["--symref"], ["HEAD"])
            match = SYMREF_RE.search(out)
            if not match:
                return ModelRemoteLaneRefResult(
                    sha=None,
                    remote=remote,
                    error="the remote names no HEAD branch",
                    refusal=EnumRemoteLaneRefRefusal.NO_DEFAULT_BRANCH,
                )
            landing = match.group(1)
            ref = request.ref or landing
            patterns = [
                f"refs/heads/{ref}",
                f"refs/tags/{ref}^{{}}",
                f"refs/tags/{ref}",
            ]
            if ref != landing:
                patterns.append(f"refs/heads/{landing}")
            out, remote = self._ls_remote(request, [], patterns)
        except _ReadFailedError as exc:
            return ModelRemoteLaneRefResult(
                sha=None,
                landing_branch=landing,
                remote=remote,
                error=str(exc),
                refusal=EnumRemoteLaneRefRefusal.READ_FAILED,
            )
        rows = [
            (value, name)
            for value, _, name in (line.partition("\t") for line in out.splitlines())
            if SHA_RE.match(value)
        ]
        if ref != landing and f"refs/heads/{landing}" not in {n for _, n in rows}:
            return ModelRemoteLaneRefResult(
                sha=None,
                landing_branch=landing,
                remote=remote,
                error=f"the remote has no default branch {landing}",
                refusal=EnumRemoteLaneRefRefusal.DEFAULT_BRANCH_NOT_ON_REMOTE,
            )
        for prefix in ("refs/heads/", "refs/tags/"):
            for value, name in rows:
                if name.startswith(prefix) and name.removeprefix(prefix) in {
                    ref,
                    f"{ref}^{{}}",
                }:
                    return ModelRemoteLaneRefResult(
                        sha=value, landing_branch=landing, remote=remote
                    )
        return ModelRemoteLaneRefResult(
            sha=None,
            landing_branch=landing,
            remote=remote,
            error=f"the remote has no branch or tag {ref}",
            refusal=(
                EnumRemoteLaneRefRefusal.NO_DEFAULT_BRANCH
                if ref == landing
                else EnumRemoteLaneRefRefusal.BRANCH_NOT_PUSHED
            ),
        )
