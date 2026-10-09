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

from ..models import SHA_RE, ModelRemoteLaneRefRequest, ModelRemoteLaneRefResult
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
        return [
            "git",
            "ls-remote",
        ], f"https://github.com/{request.owner}/{request.repo}.git"

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
        if request.sha:
            if SHA_RE.match(request.sha):
                return ModelRemoteLaneRefResult(sha=request.sha)
            return ModelRemoteLaneRefResult(
                sha=None, error="the named sha is not a 40-character hex sha"
            )
        ref = request.ref
        landing: str | None = None
        remote = ""
        try:
            if not ref:
                out, remote = self._ls_remote(request, ["--symref"], ["HEAD"])
                match = SYMREF_RE.search(out)
                if not match:
                    return ModelRemoteLaneRefResult(
                        sha=None, remote=remote, error="the remote names no HEAD branch"
                    )
                landing = ref = match.group(1)
            out, remote = self._ls_remote(
                request,
                [],
                [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"],
            )
        except _ReadFailedError as exc:
            return ModelRemoteLaneRefResult(
                sha=None, landing_branch=landing, remote=remote, error=str(exc)
            )
        rows = [line.partition("\t") for line in out.splitlines()]
        for prefix in ("refs/heads/", "refs/tags/"):
            for value, _, name in rows:
                if SHA_RE.match(value) and name.startswith(prefix):
                    return ModelRemoteLaneRefResult(
                        sha=value, landing_branch=landing, remote=remote
                    )
        return ModelRemoteLaneRefResult(
            sha=None,
            landing_branch=landing,
            remote=remote,
            error=f"the remote has no branch or tag {ref}",
        )
