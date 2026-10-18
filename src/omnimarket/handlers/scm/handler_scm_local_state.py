# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR and CI state reads for ``onex scm``, from local sources first (OMN-20913).

The PR watcher already reads every open PR and its CI on each tick and writes
the result to its state file (``$ONEX_PR_WATCHER_STATE``, else
``$OMNI_HOME/.claude_scratch/pr-watcher/state.json``). The canonical clones
(``$ONEX_SCM_CLONE_ROOT/<repo name>``, else under ``$OMNI_HOME``) hold every
merged commit. A lane's PR and CI state read is answered from those
two before any bus call, with zero GitHub requests.

Freshness is the rule ``pr_state_local.py`` and node_dod_verify's local source
use: a MERGED or CLOSED record is terminal and is served at any watcher age;
anything else is served only while the watcher's ``last_tick`` is at most
eight minutes old. An unanswered read says why, and the caller asks the bus.

``pr_heads`` (OMN-20977) answers every PR's head commit and merge outcome for
the worktree prune tools. The canonical clone supplies it over git alone, never
the GitHub API: ``git ls-remote origin refs/pull/*/head refs/heads/*`` names
each PR's head commit and each live branch's tip, and the first-parent history
of the base branches names the merged PRs (a squash subject ending ``(#N)``, or
``Merge pull request #N``). The watcher state overlays head branches, merge
times and open state where it holds them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from omnimarket.handlers.scm.handler_scm_local_git import HandlerScmLocalGit
from omnimarket.models.model_github_gateway_io import (
    ModelPrHead,
    ModelPrHeadsResult,
)
from omnimarket.models.scm import (
    EnumScmLocalRead,
    ModelScmLocalReadRequest,
    ModelScmLocalReadResult,
)

logger = logging.getLogger(__name__)

WATCHER_STATE_ENV: Final[str] = "ONEX_PR_WATCHER_STATE"
# The directory holding one canonical clone per repository name.
CLONE_ROOT_ENV: Final[str] = "ONEX_SCM_CLONE_ROOT"
WATCHER_SCHEMA: Final[int] = 1
WATCHER_MAX_AGE: Final[timedelta] = timedelta(minutes=8)
_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"MERGED", "CLOSED"})
# The remote refs pr_heads reads: every PR's head and every live branch.
_PULL_HEAD = re.compile(r"^refs/pull/([1-9][0-9]*)/head$")
_SQUASH_SUBJECT = re.compile(r"\(#([1-9][0-9]*)\)\s*$")
_MERGE_SUBJECT = re.compile(r"^Merge pull request #([1-9][0-9]*) ")
_OID = re.compile(r"^[0-9a-f]{40}$")
# Base branches whose first-parent history names merged PRs, beside the
# clone's default branch.
_PR_BASES: Final[tuple[str, ...]] = ("dev", "main")
LS_REMOTE_TIMEOUT_S: Final[int] = 120
# The facts a pr-view answer carries; the watcher's PR body is left out (it can
# be 64 KiB and `onex scm pr-view --source bus` returns it through pr_text).
_VIEW_FACTS: Final[tuple[str, ...]] = (
    "number",
    "title",
    "state",
    "draft",
    "armed",
    "author",
    "base",
    "head_ref",
    "head_sha",
    "labels",
    "created_at",
    "updated_at",
    "merged_at",
)


def default_watcher_state_path() -> Path | None:
    """``$ONEX_PR_WATCHER_STATE``, else the watcher's file under ``$OMNI_HOME``."""
    declared = os.environ.get(WATCHER_STATE_ENV)
    if declared:
        return Path(declared)
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        return None
    return Path(registry_root) / ".claude_scratch" / "pr-watcher" / "state.json"


def default_claims_dir() -> Path | None:
    """The caller's PR claim registry: where ``pr_claim_registry_cli.py`` claims.

    ``$ONEX_PR_CLAIMS_DIR``, else ``$ONEX_STATE_DIR/pr-queue/claims``, else
    ``$OMNI_HOME/.onex_state/pr-queue/claims``.
    """
    declared = os.environ.get("ONEX_PR_CLAIMS_DIR")
    if declared:
        return Path(declared)
    state_dir = os.environ.get("ONEX_STATE_DIR")
    if state_dir:
        return Path(state_dir) / "pr-queue" / "claims"
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        return None
    return Path(registry_root) / ".onex_state" / "pr-queue" / "claims"


def default_clone_path(repo: str) -> Path | None:
    """``$ONEX_SCM_CLONE_ROOT/<repo name>`` when it is a git checkout.

    A declared root is the only root read: one that holds no clone of the repo
    answers None and never falls through. A host that declares none resolves
    the clone under ``$OMNI_HOME``, which the command's help does not name.
    """
    root = os.environ.get(CLONE_ROOT_ENV) or os.environ.get("OMNI_HOME")
    if not root:
        return None
    clone = Path(root) / repo.split("/", 1)[1]
    return clone if (clone / ".git").exists() else None


def _parse_stamp(text: JsonValue) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _git(clone: Path, *args: str, timeout: int = 30) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s in %s failed: %s", args[0], clone, exc)
        return None
    return done.stdout if done.returncode == 0 else None


class HandlerScmLocalState:
    """Answer one PR or CI state read from the watcher state and the clone."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now

    def handle(self, request: ModelScmLocalReadRequest) -> ModelScmLocalReadResult:
        state = self._load(request.watcher_state_path)
        if isinstance(state, str):
            state_dict: dict[str, JsonValue] = {}
            why: str | None = state
        else:
            state_dict, why = state, None
        tick = state_dict.get("last_tick")
        tick_text = tick if isinstance(tick, str) else None
        fresh = why is None and self._fresh(tick)
        if why is None and not fresh:
            why = f"the watcher state is older than {WATCHER_MAX_AGE} (last_tick {tick_text})"

        if request.read is EnumScmLocalRead.PR_LIST:
            if why is not None:
                return self._unanswered(request, why)
            return self._list(request, state_dict, tick_text)
        if request.read is EnumScmLocalRead.PR_HEADS:
            return self._pr_heads(request, state_dict, tick_text, fresh=fresh)
        if request.read in (
            EnumScmLocalRead.FILE_CONTENTS,
            EnumScmLocalRead.COMMIT_VIEW,
        ):
            return HandlerScmLocalGit().handle(request)
        if request.read is EnumScmLocalRead.COMMIT_CHECKS:
            return self._commit_checks(request, state_dict, tick_text, fresh=fresh)

        short = request.repo.split("/", 1)[1]
        key = f"{short}#{request.pr_number}"
        record = self._dict(self._dict(state_dict.get("prs")).get(key))
        merge = self._dict(self._dict(state_dict.get("merges")).get(key))
        facts = self._dict(record.get("facts"))
        pr_state = facts.get("state") or ("MERGED" if merge else None)
        terminal = isinstance(pr_state, str) and pr_state in _TERMINAL_STATES

        if request.read is EnumScmLocalRead.PR_MERGE_COMMIT:
            return self._merge_commit(request, facts, merge, tick_text)
        if not facts:
            return self._unanswered(
                request, why or f"the watcher state holds no record of {key}"
            )
        if not terminal and not fresh:
            return self._unanswered(request, why or "the watcher state is stale")

        if request.read is EnumScmLocalRead.PR_COMMITS:
            return (
                HandlerScmLocalGit()
                .handle(
                    request,
                    head_sha=self._str(facts.get("head_sha")),
                    base=self._str(facts.get("base")),
                )
                .model_copy(update={"watcher_tick": tick_text})
            )

        result: dict[str, JsonValue] = {
            "repo": request.repo,
            "number": request.pr_number,
        }
        if request.read is EnumScmLocalRead.PR_VIEW:
            result.update({name: facts.get(name) for name in _VIEW_FACTS})
            result["class"] = record.get("cls")
            result["reason"] = record.get("reason")
        elif request.read is EnumScmLocalRead.PR_STATUS:
            result.update(
                {
                    "state": pr_state,
                    "head_sha": facts.get("head_sha"),
                    "draft": facts.get("draft"),
                    "armed": facts.get("armed"),
                    "blocked": record.get("blocked"),
                    "class": record.get("cls"),
                    "reason": record.get("reason"),
                    "handoff": record.get("handoff"),
                }
            )
        else:
            ci = self._dict(record.get("ci"))
            if not ci:
                return self._unanswered(
                    request, f"the watcher has no CI read of {key} yet"
                )
            result.update(self._ci_payload(record, facts, ci))
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="watcher",
            watcher_tick=tick_text,
            result=result,
        )

    # ------------------------------------------------------------------ reads

    def _list(
        self,
        request: ModelScmLocalReadRequest,
        state: dict[str, JsonValue],
        tick: str | None,
    ) -> ModelScmLocalReadResult:
        short = request.repo.split("/", 1)[1]
        prs: list[JsonValue] = []
        for key, value in sorted(self._dict(state.get("prs")).items()):
            if not key.startswith(f"{short}#"):
                continue
            facts = self._dict(self._dict(value).get("facts"))
            if facts.get("state") != "OPEN":
                continue
            prs.append(
                {
                    "number": facts.get("number"),
                    "title": facts.get("title"),
                    "draft": facts.get("draft"),
                    "head_sha": facts.get("head_sha"),
                    "head_ref": facts.get("head_ref"),
                    "class": self._dict(value).get("cls"),
                }
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            answered=True,
            source="watcher",
            watcher_tick=tick,
            result={"repo": request.repo, "count": len(prs), "prs": prs},
        )

    @staticmethod
    def _ci_payload(
        record: dict[str, JsonValue],
        facts: dict[str, JsonValue],
        ci: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        return {
            "head_sha": facts.get("head_sha"),
            "class": record.get("cls"),
            "read_at": ci.get("read_at"),
            "red": ci.get("red"),
            "pending": ci.get("pending"),
            "cancelled": ci.get("cancelled"),
            "red_facts": ci.get("red_facts"),
            "runs": ci.get("runs"),
            "detail": ci.get("detail"),
        }

    def _commit_checks(
        self,
        request: ModelScmLocalReadRequest,
        state: dict[str, JsonValue],
        tick: str | None,
        *,
        fresh: bool,
    ) -> ModelScmLocalReadResult:
        """The watcher's CI read of the PR whose head is ``request.sha``."""
        if request.sha is None:
            return self._unanswered(request, "commit_checks needs a sha")
        prefix = f"{request.repo.split('/', 1)[1]}#"
        held: list[tuple[int, dict[str, JsonValue], dict[str, JsonValue]]] = []
        for key, value in self._dict(state.get("prs")).items():
            record = self._dict(value)
            facts = self._dict(record.get("facts"))
            head = self._str(facts.get("head_sha"))
            suffix = key[len(prefix) :]
            if (
                key.startswith(prefix)
                and suffix.isdigit()
                and head is not None
                and head.startswith(request.sha)
                and self._dict(record.get("ci"))
            ):
                held.append((int(suffix), record, facts))
        if not held:
            return self._unanswered(
                request,
                f"the watcher holds no CI read of a {request.repo} PR whose head "
                f"starts {request.sha}",
            )
        # The newest PR number wins when two PRs share a head.
        number, record, facts = max(held, key=lambda item: item[0])
        terminal = facts.get("state") in _TERMINAL_STATES
        if not terminal and not fresh:
            return self._unanswered(
                request,
                f"the watcher state is older than {WATCHER_MAX_AGE} (last_tick {tick})",
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=number,
            answered=True,
            source="watcher",
            watcher_tick=tick,
            result={
                "repo": request.repo,
                "sha": request.sha,
                "number": number,
                **self._ci_payload(record, facts, self._dict(record.get("ci"))),
            },
        )

    def _merge_commit(
        self,
        request: ModelScmLocalReadRequest,
        facts: dict[str, JsonValue],
        merge: dict[str, JsonValue],
        tick: str | None,
    ) -> ModelScmLocalReadResult:
        sha = merge.get("merge_sha")
        if isinstance(sha, str) and sha:
            return ModelScmLocalReadResult(
                read=request.read,
                repo=request.repo,
                pr_number=request.pr_number,
                answered=True,
                source="watcher",
                watcher_tick=tick,
                result={
                    "repo": request.repo,
                    "number": request.pr_number,
                    "merged": True,
                    "merge_commit_sha": sha,
                },
            )
        base = facts.get("base") or merge.get("base")
        squash = self._squash_commit(
            request, base if isinstance(base, str) and base else None
        )
        if squash is None:
            return self._unanswered(
                request,
                "neither the watcher nor the canonical clone names a merge commit",
            )
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=True,
            source="clone",
            result={
                "repo": request.repo,
                "number": request.pr_number,
                "merged": True,
                "merge_commit_sha": squash,
            },
        )

    def _squash_commit(
        self, request: ModelScmLocalReadRequest, base: str | None
    ) -> str | None:
        """The one first-parent commit on ``origin/<base>`` ending ``(#<n>)``."""
        if request.clone_path is None:
            return None
        clone = Path(request.clone_path)
        if base is None:
            head = _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            if head is None or not head.strip().startswith("origin/"):
                return None
            base = head.strip()[len("origin/") :]
        suffix = f"(#{request.pr_number})"
        out = _git(
            clone,
            "log",
            "--first-parent",
            "--fixed-strings",
            f"--grep={suffix}",
            "--format=%H%x09%s",
            f"refs/remotes/origin/{base}",
        )
        if out is None:
            return None
        shas = [
            line.split("\t", 1)[0]
            for line in out.splitlines()
            if "\t" in line and line.split("\t", 1)[1].rstrip().endswith(suffix)
        ]
        return shas[0] if len(shas) == 1 else None

    def _pr_heads(
        self,
        request: ModelScmLocalReadRequest,
        state: dict[str, JsonValue],
        tick: str | None,
        *,
        fresh: bool,
    ) -> ModelScmLocalReadResult:
        """Every PR's head commit and merge outcome, from the clone over git."""
        if request.clone_path is None:
            return self._unanswered(
                request, f"no canonical clone of {request.repo} on this host"
            )
        clone = Path(request.clone_path)
        remote = _git(
            clone,
            "ls-remote",
            "origin",
            "refs/pull/*/head",
            "refs/heads/*",
            timeout=LS_REMOTE_TIMEOUT_S,
        )
        if remote is None:
            return self._unanswered(
                request, f"git ls-remote origin failed in the clone {clone}"
            )
        pull_heads: dict[int, str] = {}
        branches_at: dict[str, list[str]] = {}
        for line in remote.splitlines():
            oid, _, ref = line.partition("\t")
            if not _OID.match(oid):
                continue
            pull = _PULL_HEAD.match(ref)
            if pull is not None:
                pull_heads[int(pull.group(1))] = oid
            elif ref.startswith("refs/heads/"):
                branches_at.setdefault(oid, []).append(ref[len("refs/heads/") :])
        merged = self._merged_in_history(clone)

        short = request.repo.split("/", 1)[1]
        prefix = f"{short}#"
        watched: dict[int, dict[str, JsonValue]] = {}
        for key, value in self._dict(state.get("prs")).items():
            if key.startswith(prefix) and key[len(prefix) :].isdigit():
                watched[int(key[len(prefix) :])] = self._dict(
                    self._dict(value).get("facts")
                )
        watched_merges: dict[int, dict[str, JsonValue]] = {
            int(key[len(prefix) :]): self._dict(value)
            for key, value in self._dict(state.get("merges")).items()
            if key.startswith(prefix) and key[len(prefix) :].isdigit()
        }

        prs: list[ModelPrHead] = []
        for number in sorted(
            set(pull_heads) | set(watched) | set(watched_merges), reverse=True
        ):
            pr = self._pr_head(
                number,
                pull_heads.get(number),
                merged.get(number),
                watched.get(number, {}),
                watched_merges.get(number, {}),
                branches_at,
                fresh=fresh,
            )
            if pr is None:
                continue
            if request.branch is not None and pr.head_ref != request.branch:
                continue
            if request.head_sha is not None and pr.head_oid != request.head_sha:
                continue
            prs.append(pr)
        answer = ModelPrHeadsResult(repo=request.repo, prs=prs, truncated=False)
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            answered=True,
            source="clone",
            watcher_tick=tick,
            result=answer.model_dump(mode="json"),
        )

    def _pr_head(
        self,
        number: int,
        pull_head: str | None,
        merge_sha: str | None,
        facts: dict[str, JsonValue],
        merge: dict[str, JsonValue],
        branches_at: dict[str, list[str]],
        *,
        fresh: bool,
    ) -> ModelPrHead | None:
        head = self._str(merge.get("head_sha")) or self._str(facts.get("head_sha"))
        head = head if head is not None and _OID.match(head) else pull_head
        if head is None:
            return None
        watched_state = self._str(facts.get("state"))
        state: Literal["MERGED", "OPEN", "CLOSED", "UNMERGED"]
        if merge or merge_sha is not None or watched_state == "MERGED":
            state = "MERGED"
        elif watched_state == "CLOSED":
            state = "CLOSED"
        elif watched_state == "OPEN" and fresh:
            state = "OPEN"
        else:
            state = "UNMERGED"
        head_ref = self._str(merge.get("head_ref")) or self._str(facts.get("head_ref"))
        if head_ref is None:
            named = branches_at.get(head, [])
            head_ref = named[0] if len(named) == 1 else None
        sha = self._str(merge.get("merge_sha")) or merge_sha
        return ModelPrHead(
            number=number,
            state=state,
            head_oid=head,
            head_ref=head_ref,
            base_ref=self._str(merge.get("base")) or self._str(facts.get("base")),
            merge_commit_sha=sha if sha is not None and _OID.match(sha) else None,
            merged_at=self._str(merge.get("merged_at"))
            or self._str(facts.get("merged_at")),
        )

    def _merged_in_history(self, clone: Path) -> dict[int, str]:
        """PR number to merge commit, from the base branches' first-parent history."""
        bases = list(_PR_BASES)
        default = _git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if default is not None and default.strip().startswith("origin/"):
            bases.insert(0, default.strip()[len("origin/") :])
        merged: dict[int, str] = {}
        for base in dict.fromkeys(bases):
            out = _git(
                clone,
                "log",
                "--first-parent",
                "--format=%H%x09%s",
                f"refs/remotes/origin/{base}",
                timeout=LS_REMOTE_TIMEOUT_S,
            )
            for line in (out or "").splitlines():
                sha, _, subject = line.partition("\t")
                found = _SQUASH_SUBJECT.search(subject) or _MERGE_SUBJECT.match(subject)
                if found is not None:
                    merged.setdefault(int(found.group(1)), sha)
        return merged

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _str(value: JsonValue | None) -> str | None:
        return value if isinstance(value, str) and value else None

    def _load(self, path_text: str | None) -> dict[str, JsonValue] | str:
        if path_text is None:
            return "no PR watcher state file is known on this host"
        path = Path(path_text)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return f"the watcher state {path} is unreadable: {exc}"
        if not isinstance(raw, dict) or raw.get("schema") != WATCHER_SCHEMA:
            return f"the watcher state {path} is not schema {WATCHER_SCHEMA}"
        return raw

    def _fresh(self, tick: JsonValue) -> bool:
        last = _parse_stamp(tick)
        if last is None:
            return False
        return (self._now or datetime.now(UTC)) - last <= WATCHER_MAX_AGE

    @staticmethod
    def _dict(value: JsonValue | None) -> dict[str, JsonValue]:
        return value if isinstance(value, dict) else {}

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


__all__: list[str] = [
    "CLONE_ROOT_ENV",
    "WATCHER_MAX_AGE",
    "HandlerScmLocalState",
    "default_claims_dir",
    "default_clone_path",
    "default_watcher_state_path",
]
