# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub PR reads for node_linear_triage from local sources, with no GitHub request.

A lane host holds no GitHub token and lanes do not read GitHub. The PR watcher
state (``$ONEX_PR_WATCHER_STATE``) already holds every open PR and the recently
merged and closed ones with their titles and bodies, and each canonical clone
holds every merged squash commit. :class:`LocalGitHubPrReads` answers the
``GitHubClientProtocol`` reads the triage handler makes from those two sources.

A read it cannot answer is never silent: a missing watcher state raises
:class:`LocalGitHubStateError`, and a repository without a canonical clone is
listed in :attr:`LocalGitHubPrReads.unread_repos`. ``pr_closing_ticket_refs``
(a GitHub-only link) answers an empty list, so the handler's fail-closed
implementing-PR gate holds a ticket whose only signal is that link.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnimarket.handlers.scm.handler_scm_local_state import (
    default_clone_path,
    default_watcher_state_path,
)

_log = logging.getLogger(__name__)

_ORG = "OmniNode-ai"
_SQUASH_SUBJECT = re.compile(r"^(?P<title>.*?)\s*\(#(?P<number>[1-9][0-9]*)\)\s*$")
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_GIT_TIMEOUT_S = 60


class LocalGitHubStateError(RuntimeError):
    """The local PR sources cannot answer at all (no readable watcher state)."""


def _term_pattern(search_term: str) -> re.Pattern[str]:
    """Whole-token match of ``search_term`` (an id ending ``-12`` must not match ``-123``)."""
    return re.compile(
        rf"(?<![A-Za-z0-9-]){re.escape(search_term)}(?![A-Za-z0-9])", re.IGNORECASE
    )


class LocalGitHubPrReads:
    """``GitHubClientProtocol`` backed by the watcher state and canonical clones."""

    def __init__(
        self,
        *,
        watcher_state_path: Path | None = None,
        clone_for: Callable[[str], Path | None] = default_clone_path,
    ) -> None:
        path = watcher_state_path or default_watcher_state_path()
        if path is None or not Path(path).is_file():
            raise LocalGitHubStateError(
                "no PR watcher state to read: set ONEX_PR_WATCHER_STATE or OMNI_HOME"
            )
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LocalGitHubStateError(
                f"PR watcher state {path} is unreadable: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise LocalGitHubStateError(f"PR watcher state {path} is not an object")
        self._clone_for = clone_for
        self.watcher_tick: str = str(state.get("last_tick") or "")
        self.unread_repos: set[str] = set()
        self._git_cache: dict[str, list[dict[str, str]]] = {}
        self._watcher_prs = self._load_watcher(state)
        self._repos = sorted(
            {pr["repo"] for pr in self._watcher_prs.values()}
            | {str(r) for r in (state.get("repos") or {})}
        )

    # ------------------------------------------------------------- sources

    @staticmethod
    def _load_watcher(state: dict[str, Any]) -> dict[tuple[str, int], dict[str, str]]:
        found: dict[tuple[str, int], dict[str, str]] = {}
        merges = state.get("merges") or {}
        for key, rec in (state.get("prs") or {}).items():
            facts = (rec or {}).get("facts") or {}
            repo, _, num = str(key).partition("#")
            if not num.isdigit() or not facts:
                continue
            raw = str(facts.get("state") or "").upper()
            merged_at = str(
                facts.get("merged_at") or (merges.get(key) or {}).get("merged_at") or ""
            )
            if raw == "MERGED" and not merged_at:
                _log.warning("%s is MERGED with no merged_at; skipped", key)
                continue
            found[(repo, int(num))] = {
                "number": num,
                "title": str(facts.get("title") or ""),
                "body": str(facts.get("body") or ""),
                "state": "open" if raw == "OPEN" else "closed",
                "mergedAt": merged_at if raw == "MERGED" else "",
                "url": f"https://github.com/{_ORG}/{repo}/pull/{num}",
                "repo": repo,
                "head_ref": str(facts.get("head_ref") or ""),
            }
        for key, merge in merges.items():
            repo, _, num = str(key).partition("#")
            if (
                not num.isdigit()
                or (repo, int(num)) in found
                or not merge.get("merged_at")
            ):
                continue
            found[(repo, int(num))] = {
                "number": num,
                "title": str(merge.get("title") or ""),
                "body": "",
                "state": "closed",
                "mergedAt": str(merge["merged_at"]),
                "url": f"https://github.com/{_ORG}/{repo}/pull/{num}",
                "repo": repo,
                "head_ref": str(merge.get("head_ref") or ""),
            }
        return found

    def _git_merged(self, repo: str) -> list[dict[str, str]]:
        """Squash-merged PRs on the clone's default branch (subject ends ``(#N)``)."""
        if repo in self._git_cache:
            return self._git_cache[repo]
        merged: list[dict[str, str]] = []
        clone = self._clone_for(f"{_ORG}/{repo}")
        if clone is None:
            self.unread_repos.add(repo)
            self._git_cache[repo] = merged
            return merged
        head = self._git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        ref = (
            f"refs/remotes/{head.strip()}"
            if head and head.strip().startswith("origin/")
            else "refs/remotes/origin/dev"
        )
        out = self._git(
            clone,
            "log",
            "--first-parent",
            f"--format=%cI{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}",
            ref,
        )
        if out is None:
            self.unread_repos.add(repo)
        for record in (out or "").split(_RECORD_SEP):
            parts = record.strip("\n").split(_FIELD_SEP)
            if len(parts) != 3:
                continue
            squash = _SQUASH_SUBJECT.match(parts[1])
            if squash is None:
                continue
            merged.append(
                {
                    "number": squash.group("number"),
                    "title": squash.group("title"),
                    "body": parts[2],
                    "state": "closed",
                    "mergedAt": parts[0],
                    "url": f"https://github.com/{_ORG}/{repo}/pull/{squash.group('number')}",
                    "repo": repo,
                    "head_ref": "",
                }
            )
        self._git_cache[repo] = merged
        return merged

    @staticmethod
    def _git(clone: Path, *args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(clone), *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log.warning("git %s in %s failed: %s", args[0], clone, exc)
            return None
        if done.returncode != 0:
            _log.warning("git %s in %s exited %d", args[0], clone, done.returncode)
            return None
        return done.stdout

    # ------------------------------------------------------------- protocol

    def _candidates(self, repo: str | None) -> list[dict[str, str]]:
        repos = [repo] if repo else self._repos
        out: dict[tuple[str, str], dict[str, str]] = {}
        for name in repos:
            for pr in self._git_merged(name):
                out[(name, pr["number"])] = pr
        for (name, num), pr in self._watcher_prs.items():
            if repo and name != repo:
                continue
            out[(name, str(num))] = pr
        return list(out.values())

    @staticmethod
    def _wanted(pr: dict[str, str], state: str) -> bool:
        if state == "merged":
            return bool(pr["mergedAt"])
        if state == "closed":
            return pr["state"] == "closed" and not pr["mergedAt"]
        if state == "open":
            return pr["state"] == "open"
        return True

    @staticmethod
    def _public(pr: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in pr.items() if k != "head_ref"}

    def _search(
        self, repo: str | None, search_term: str, state: str
    ) -> list[dict[str, str]]:
        pattern = _term_pattern(search_term)
        hits = [
            pr
            for pr in self._candidates(repo)
            if self._wanted(pr, state) and pattern.search(f"{pr['title']} {pr['body']}")
        ]
        hits.sort(key=lambda p: p["mergedAt"], reverse=True)
        return [self._public(pr) for pr in hits[:10]]

    def search_prs(
        self, *, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        return self._search(None, search_term, state)

    def search_prs_in_repo(
        self, *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        return self._search(repo, search_term, state)

    def list_prs_by_head(
        self, *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]:
        hits = [
            pr
            for (name, _), pr in self._watcher_prs.items()
            if name == repo and pr["head_ref"] == branch and self._wanted(pr, state)
        ]
        return [self._public(pr) for pr in hits[:5]]

    def pr_closing_ticket_refs(self, *, repo: str, number: int) -> list[str]:
        return []
