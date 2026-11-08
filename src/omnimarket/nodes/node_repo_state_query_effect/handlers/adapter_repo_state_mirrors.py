# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployed adapters of node_repo_state_query_effect (OMN-23033).

* :class:`GitCloneMirror` reads a canonical clone with local git only. Every command runs
  with ``GIT_TERMINAL_PROMPT=0`` and ``GIT_NO_LAZY_FETCH=1``, so no read can reach the
  remote, even on a partial clone. It never fetches, checks out or writes.
* :class:`PrWatcherStateFile` reads the PR watcher's state file.
* :class:`FoldedBusRepoState` folds the merge events the bus carries into the latest head
  per branch and the latest event per pull request, and persists the fold so a restart
  keeps it.
* :class:`CommandMirrorPuller` pulls: the clone through the canonical clone refresh
  node's handler (the host's own sync command, which fetches over git and only
  fast-forwards), the watcher through the host's watcher refresh command.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import ValidationError

from omnimarket.models.model_repo_merged import validate_git_sha, validate_repo_slug
from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    EnumCanonicalCloneRefreshStatus,
    ModelCanonicalCloneRefreshReceipt,
    ModelCanonicalCloneRefreshRequest,
)
from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateMirror,
    ModelBusPrEvent,
    ModelBusRepoHead,
    ModelMirrorPull,
    ModelRepoStateFile,
    is_full_sha,
)

REPO_PLACEHOLDER = "{repo}"
Runner = Callable[..., Any]


def clones_under(root: Path) -> Callable[[str], Path | None]:
    """A repository's canonical clone is ``<root>/<name>`` when that is a git clone."""

    def clone_for(repo: str) -> Path | None:
        path = root / repo.split("/", 1)[1]
        return path if (path / ".git").exists() else None

    return clone_for


class GitCloneMirror:
    """Read-only, local-only reads of this host's canonical clones."""

    def __init__(
        self,
        clone_for: Callable[[str], Path | None],
        *,
        timeout_seconds: float,
        max_file_chars: int,
        run: Runner = subprocess.run,
    ) -> None:
        self._clone_for = clone_for
        self._timeout = timeout_seconds
        self._max_chars = max_file_chars
        self._run = run
        self._env = {
            **scrub_git_location_env(os.environ),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
        }

    def _git(self, repo: str, *args: str) -> subprocess.CompletedProcess[str] | None:
        path = self._clone_for(repo)
        if path is None:
            return None
        try:
            done: subprocess.CompletedProcess[str] = self._run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=self._env,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done

    def default_branch(self, repo: str) -> str | None:
        done = self._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if done is None or done.returncode:
            return None
        return done.stdout.strip().removeprefix("origin/") or None

    def refs(self, repo: str) -> dict[str, str] | None:
        done = self._git(
            repo,
            "for-each-ref",
            "--format=%(refname:strip=3) %(objectname)",
            "refs/remotes/origin",
        )
        if done is None or done.returncode:
            return None
        refs: dict[str, str] = {}
        for line in done.stdout.splitlines():
            name, _, sha = line.partition(" ")
            if name and name != "HEAD" and is_full_sha(sha):
                refs[name] = sha
        return refs

    def contains(self, repo: str, branch: str, sha: str) -> bool:
        done = self._git(
            repo, "merge-base", "--is-ancestor", sha, f"refs/remotes/origin/{branch}"
        )
        return done is not None and done.returncode == 0

    def resolve(self, repo: str, revision: str) -> str | None:
        ref = revision if is_full_sha(revision) else f"refs/remotes/origin/{revision}"
        done = self._git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if done is None or done.returncode:
            return None
        sha = done.stdout.strip()
        return sha if is_full_sha(sha) else None

    def read_files(
        self, repo: str, sha: str, paths: tuple[str, ...]
    ) -> tuple[ModelRepoStateFile, ...]:
        out: list[ModelRepoStateFile] = []
        for path in paths:
            done = self._git(repo, "show", f"{sha}:{path}")
            if done is None or done.returncode:
                out.append(ModelRepoStateFile(path=path, present=False))
                continue
            text = done.stdout
            out.append(
                ModelRepoStateFile(
                    path=path,
                    present=True,
                    content=text[: self._max_chars],
                    truncated=len(text) > self._max_chars,
                )
            )
        return tuple(out)


class PrWatcherStateFile:
    """The PR watcher's state file: ``last_tick`` and ``prs`` keyed ``<name>#<n>``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def read_at(self) -> datetime | None:
        data = self._load()
        tick = data.get("last_tick") if data else None
        if not isinstance(tick, str):
            return None
        try:
            parsed = datetime.fromisoformat(tick.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def record(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        data = self._load()
        prs = data.get("prs") if data else None
        if not isinstance(prs, dict):
            return None
        rec = prs.get(f"{repo.split('/', 1)[1]}#{pr_number}")
        return rec if isinstance(rec, dict) else None


BRANCH_ADVANCED_KIND = "branch-ref-advanced"


def _as_merge_payload(push: Mapping[str, Any]) -> dict[str, Any]:
    """A push to a watched branch (``onex.evt.github.branch-head.v1``) as a head advance.

    The webhook ingress folds push deliveries into ``branch-ref-advanced`` observations: the
    branch's new sha and the time GitHub says it held. A push to a branch that is never merged
    still advances that branch's head here, so a mirror that lacks it reads as behind.
    """
    return {
        "repo": push.get("repo"),
        "base": push.get("branch"),
        "merge_sha": push.get("sha"),
        "merged_at": push.get("as_of"),
        "state": "merged",
        "source": "push",
    }


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FoldedBusRepoState:
    """The latest head per branch and event per pull request the bus has carried.

    Reads two payload shapes: ``onex.evt.omnimarket.repo-merged.v1`` (``base``, ``state``)
    and ``onex.evt.github.pr-merged.v1`` (``base_ref``). An older event never replaces a
    newer one. ``state_path`` persists the fold after every change.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._path = state_path
        self._lock = threading.Lock()
        self._heads: dict[tuple[str, str], ModelBusRepoHead] = {}
        self._prs: dict[tuple[str, int], ModelBusPrEvent] = {}
        self.folded = 0
        self.skipped = 0
        if state_path is not None and state_path.is_file():
            self._restore(state_path)

    def _restore(self, path: Path) -> None:
        with contextlib.suppress(
            OSError, UnicodeError, json.JSONDecodeError, ValidationError
        ):
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("heads", []):
                head = ModelBusRepoHead.model_validate(item)
                self._heads[(head.repo, head.branch)] = head
            for item in raw.get("prs", []):
                pr = ModelBusPrEvent.model_validate(item)
                self._prs[(pr.repo, pr.pr_number)] = pr

    def apply(self, payload: Mapping[str, Any]) -> bool:
        """Fold one event payload; return whether it changed the fold."""
        if payload.get("kind") == BRANCH_ADVANCED_KIND:
            payload = _as_merge_payload(payload)
        try:
            repo = validate_repo_slug(str(payload.get("repo", "")))
            sha = validate_git_sha(str(payload.get("merge_sha", "")))
        except ValueError:
            self.skipped += 1
            return False
        branch = payload.get("base") or payload.get("base_ref")
        at = _parse_time(payload.get("merged_at"))
        state = str(payload.get("state") or "merged")
        pr_number = payload.get("pr_number")
        source = str(payload.get("source") or "bus")[:64]
        if not isinstance(branch, str) or not branch or at is None:
            self.skipped += 1
            return False
        changed = False
        with self._lock:
            if state == "merged":
                old = self._heads.get((repo, branch))
                if old is None or old.observed_at < at:
                    self._heads[(repo, branch)] = ModelBusRepoHead(
                        repo=repo, branch=branch, sha=sha, observed_at=at, source=source
                    )
                    changed = True
            if isinstance(pr_number, int) and pr_number >= 1:
                old_pr = self._prs.get((repo, pr_number))
                if old_pr is None or old_pr.observed_at < at:
                    self._prs[(repo, pr_number)] = ModelBusPrEvent(
                        repo=repo,
                        pr_number=pr_number,
                        state=state[:16],
                        sha=sha,
                        observed_at=at,
                    )
                    changed = True
            if changed:
                self.folded += 1
                self._persist()
        return changed

    def _persist(self) -> None:
        if self._path is None:
            return
        doc = {
            "heads": [h.model_dump(mode="json") for h in self._heads.values()],
            "prs": [p.model_dump(mode="json") for p in self._prs.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=self._path.name, suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        os.replace(tmp, self._path)

    def head(self, repo: str, branch: str) -> ModelBusRepoHead | None:
        with self._lock:
            return self._heads.get((repo, branch))

    def heads(self, repo: str) -> tuple[ModelBusRepoHead, ...]:
        with self._lock:
            return tuple(h for (r, _), h in self._heads.items() if r == repo)

    def pr_event(self, repo: str, pr_number: int) -> ModelBusPrEvent | None:
        with self._lock:
            return self._prs.get((repo, pr_number))


class _CloneRefresh(Protocol):
    """What the puller needs of the canonical clone refresh node's handler."""

    def handle(
        self, request: ModelCanonicalCloneRefreshRequest
    ) -> ModelCanonicalCloneRefreshReceipt: ...


_CLONE_OK = {
    EnumCanonicalCloneRefreshStatus.ADVANCED,
    EnumCanonicalCloneRefreshStatus.UP_TO_DATE,
}


class CommandMirrorPuller:
    """Pull the clone through the clone refresh handler; the watcher through its command."""

    def __init__(
        self,
        clone_refresh: _CloneRefresh,
        *,
        watcher_argv: Sequence[str] | None,
        timeout_seconds: float,
        run: Runner = subprocess.run,
    ) -> None:
        if watcher_argv is not None and not any(
            REPO_PLACEHOLDER in p for p in watcher_argv
        ):
            raise ValueError(
                f"the watcher refresh command must name the repository as {REPO_PLACEHOLDER}"
            )
        self._clone_refresh = clone_refresh
        self._watcher_argv = list(watcher_argv) if watcher_argv is not None else None
        self._timeout = timeout_seconds
        self._run = run

    def pull(
        self, repo: str, mirror: EnumRepoStateMirror, target_sha: str
    ) -> ModelMirrorPull:
        if mirror is EnumRepoStateMirror.CLONE:
            return self._pull_clone(repo, target_sha)
        return self._pull_watcher(repo, target_sha)

    def _pull_clone(self, repo: str, target_sha: str) -> ModelMirrorPull:
        receipt = self._clone_refresh.handle(
            ModelCanonicalCloneRefreshRequest(
                repo=repo, target_sha=target_sha, events=1, sources=["repo-state-query"]
            )
        )
        return ModelMirrorPull(
            repo=repo,
            mirror=EnumRepoStateMirror.CLONE,
            status=receipt.status.value,
            ok=receipt.status in _CLONE_OK,
            target_sha=target_sha,
            before_sha=receipt.before_sha,
            after_sha=receipt.after_sha,
            duration_ms=receipt.duration_ms,
            message=receipt.message,
        )

    def _pull_watcher(self, repo: str, target_sha: str) -> ModelMirrorPull:
        if self._watcher_argv is None:
            return ModelMirrorPull(
                repo=repo,
                mirror=EnumRepoStateMirror.WATCHER,
                status="not_configured",
                ok=False,
                target_sha=target_sha,
                message="this host declares no watcher refresh command",
            )
        argv = [p.replace(REPO_PLACEHOLDER, repo) for p in self._watcher_argv]
        started = time.monotonic()
        try:
            done = self._run(
                argv, capture_output=True, text=True, timeout=self._timeout, check=False
            )
            ok, tail = (
                done.returncode == 0,
                (done.stderr or done.stdout or "").strip()[-500:],
            )
        except (OSError, subprocess.SubprocessError) as exc:
            ok, tail = False, f"{type(exc).__name__}: {exc}"
        return ModelMirrorPull(
            repo=repo,
            mirror=EnumRepoStateMirror.WATCHER,
            status="refreshed" if ok else "failed",
            ok=ok,
            target_sha=target_sha,
            duration_ms=int((time.monotonic() - started) * 1000),
            message=tail,
        )


__all__ = [
    "CommandMirrorPuller",
    "FoldedBusRepoState",
    "GitCloneMirror",
    "PrWatcherStateFile",
    "clones_under",
]
