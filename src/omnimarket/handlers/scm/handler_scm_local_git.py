# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository reads for ``onex scm``, answered from the canonical clone (OMN-20913).

A lane reads a file, a commit or a PR's commit list from the clone that
canonical-clone-sync keeps fetched, over git alone: zero GitHub requests. The
source-control gateway node declares no operation for these reads, so a read
the clone cannot answer says why and is not sent anywhere.

Every caller-supplied ref, sha and path reaches git only after the request
model has validated it, and a ref is resolved to a fully qualified name
(``refs/remotes/origin/<ref>``, ``refs/tags/<ref>``) or a hex object name, so
none can be read as a git option.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from omnimarket.models.scm import (
    EnumScmLocalRead,
    ModelScmLocalReadRequest,
    ModelScmLocalReadResult,
)

logger = logging.getLogger(__name__)

_HEX_OBJECT: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{7,40}$")
_OID: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
# The environment names that point git at a repository other than the clone.
_GIT_LOCATION_ENV: Final[frozenset[str]] = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"}
)
_FIELD_SEPARATOR: Final[str] = "\x1f"
_RECORD_SEPARATOR: Final[str] = "\x1e"
_COMMIT_FORMAT: Final[str] = _FIELD_SEPARATOR.join(("%H", "%P", "%an", "%aI", "%cI"))
COMMIT_LIST_LIMIT: Final[int] = 250


def _git(clone: Path, *args: str, timeout: int = 30) -> bytes | None:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s in %s failed: %s", args[0], clone, exc)
        return None
    return done.stdout if done.returncode == 0 else None


def _text(out: bytes | None) -> str | None:
    return out.decode("utf-8", errors="replace") if out is not None else None


class HandlerScmLocalGit:
    """Answer one file, commit or PR-commit-list read from the canonical clone."""

    def handle(
        self,
        request: ModelScmLocalReadRequest,
        *,
        head_sha: str | None = None,
        base: str | None = None,
    ) -> ModelScmLocalReadResult:
        """``head_sha`` and ``base`` are the watcher's facts, for ``pr_commits``."""
        if request.clone_path is None:
            return self._unanswered(
                request, f"no canonical clone of {request.repo} on this host"
            )
        clone = Path(request.clone_path)
        if request.read is EnumScmLocalRead.FILE_CONTENTS:
            return self._file_contents(request, clone)
        if request.read is EnumScmLocalRead.COMMIT_VIEW:
            return self._commit_view(request, clone)
        if request.read is EnumScmLocalRead.PR_COMMITS:
            return self._pr_commits(request, clone, head_sha, base)
        raise ValueError(f"{request.read.value} is not a clone read")

    # ------------------------------------------------------------------ reads

    def _file_contents(
        self, request: ModelScmLocalReadRequest, clone: Path
    ) -> ModelScmLocalReadResult:
        if request.path is None:
            return self._unanswered(request, "file_contents needs a path")
        commit = self._resolve(clone, request.ref)
        if commit is None:
            return self._unanswered(
                request,
                f"the clone {clone} holds no commit for ref {request.ref or 'origin/HEAD'}",
            )
        spec = f"{commit}:{request.path}"
        kind = _text(_git(clone, "cat-file", "-t", spec))
        if kind is None:
            return self._unanswered(
                request, f"{request.path} is not in the tree of {commit}"
            )
        result: dict[str, JsonValue] = {
            "repo": request.repo,
            "ref": request.ref,
            "commit": commit,
            "path": request.path,
            "type": kind.strip(),
        }
        if kind.strip() == "tree":
            listing = _text(_git(clone, "ls-tree", "-z", "--long", spec)) or ""
            result["entries"] = [
                self._tree_entry(entry) for entry in listing.split("\0") if entry
            ]
        elif kind.strip() == "blob":
            blob = _git(clone, "cat-file", "blob", spec)
            if blob is None:
                return self._unanswered(request, f"{spec} could not be read")
            body = blob[: request.max_bytes]
            result["total_bytes"] = len(blob)
            result["truncated"] = len(blob) > request.max_bytes
            try:
                result["content"] = body.decode("utf-8")
                result["encoding"] = "utf-8"
            except UnicodeDecodeError:
                result["content"] = base64.b64encode(body).decode("ascii")
                result["encoding"] = "base64"
        else:
            return self._unanswered(
                request, f"{request.path} is a {kind.strip()}, not a file or directory"
            )
        return self._answered(request, result)

    def _commit_view(
        self, request: ModelScmLocalReadRequest, clone: Path
    ) -> ModelScmLocalReadResult:
        if request.ref is None:
            return self._unanswered(request, "commit_view needs a ref")
        commit = self._resolve(clone, request.ref)
        if commit is None:
            return self._unanswered(
                request, f"the clone {clone} holds no commit for ref {request.ref}"
            )
        header = _text(
            _git(clone, "show", "-s", f"--format={_COMMIT_FORMAT}%x1e%B", commit)
        )
        if header is None:
            return self._unanswered(request, f"git show failed for {commit}")
        meta, _, message = header.partition(_RECORD_SEPARATOR)
        sha, parents, author, authored, committed = meta.split(_FIELD_SEPARATOR)
        files = _text(
            _git(
                clone,
                "show",
                "-m",
                "--first-parent",
                "--name-status",
                "--format=",
                commit,
            )
        )
        changed: list[JsonValue] = []
        for line in (files or "").splitlines():
            status, _, name = line.partition("\t")
            if name:
                changed.append({"status": status, "path": name})
        return self._answered(
            request,
            {
                "repo": request.repo,
                "sha": sha,
                "parents": [*parents.split()],
                "author": author,
                "authored_at": authored,
                "committed_at": committed,
                "message": message.strip("\n"),
                "files": changed,
            },
        )

    def _pr_commits(
        self,
        request: ModelScmLocalReadRequest,
        clone: Path,
        head_sha: str | None,
        base: str | None,
    ) -> ModelScmLocalReadResult:
        if head_sha is None or base is None:
            return self._unanswered(
                request, "the watcher state names no head commit or base branch"
            )
        base_ref = self._resolve(clone, base)
        if (
            base_ref is None
            or _git(clone, "cat-file", "-e", f"{head_sha}^{{commit}}") is None
        ):
            return self._unanswered(
                request,
                f"the clone {clone} does not hold both origin/{base} and {head_sha}",
            )
        listing = _text(
            _git(
                clone,
                "log",
                "--reverse",
                f"--max-count={COMMIT_LIST_LIMIT + 1}",
                f"--format={_COMMIT_FORMAT}{_RECORD_SEPARATOR}%s",
                f"{base_ref}..{head_sha}",
            )
        )
        if listing is None:
            return self._unanswered(request, "git log of the PR range failed")
        commits: list[JsonValue] = []
        for record in listing.split("\n"):
            if not record:
                continue
            meta, _, subject = record.partition(_RECORD_SEPARATOR)
            sha, parents, author, authored, committed = meta.split(_FIELD_SEPARATOR)
            commits.append(
                {
                    "sha": sha,
                    "parents": [*parents.split()],
                    "author": author,
                    "authored_at": authored,
                    "committed_at": committed,
                    "subject": subject,
                }
            )
        truncated = len(commits) > COMMIT_LIST_LIMIT
        return self._answered(
            request,
            {
                "repo": request.repo,
                "number": request.pr_number,
                "base": base,
                "head_sha": head_sha,
                "count": min(len(commits), COMMIT_LIST_LIMIT),
                "truncated": truncated,
                "commits": commits[:COMMIT_LIST_LIMIT],
            },
        )

    # ---------------------------------------------------------------- helpers

    def _resolve(self, clone: Path, ref: str | None) -> str | None:
        """The full commit sha ``ref`` names in the clone, or None.

        No ref means the clone's default branch (``origin/HEAD``). A name is tried
        as a commit (when it is hex), then as a remote branch, then as a tag.
        """
        candidates: list[str] = []
        if ref is None:
            head = _text(
                _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            )
            if head is None or not head.strip().startswith("origin/"):
                return None
            candidates.append(f"refs/remotes/{head.strip()}")
        else:
            if _HEX_OBJECT.match(ref):
                candidates.append(ref)
            candidates.extend((f"refs/remotes/origin/{ref}", f"refs/tags/{ref}"))
            if ref.startswith("origin/"):
                candidates.append(f"refs/remotes/{ref}")
        for candidate in candidates:
            out = _text(
                _git(
                    clone, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"
                )
            )
            if out is not None and _OID.match(out.strip()):
                return out.strip()
        return None

    @staticmethod
    def _tree_entry(line: str) -> JsonValue:
        meta, _, name = line.partition("\t")
        mode, kind, sha, size = meta.split()
        return {
            "name": name,
            "type": kind,
            "mode": mode,
            "sha": sha,
            "size": None if size == "-" else int(size),
        }

    @staticmethod
    def _answered(
        request: ModelScmLocalReadRequest, result: dict[str, JsonValue]
    ) -> ModelScmLocalReadResult:
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="clone",
            result=result,
        )

    @staticmethod
    def _unanswered(
        request: ModelScmLocalReadRequest, why: str
    ) -> ModelScmLocalReadResult:
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=False,
            unanswered_reason=why,
        )


__all__: list[str] = ["COMMIT_LIST_LIMIT", "HandlerScmLocalGit"]
