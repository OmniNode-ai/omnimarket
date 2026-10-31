# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The snapshot the duplicate-fix pull request guard folds over (OMN-17427).

Reads, never searches GitHub: the PR watcher's state file for the open pull requests of one repository,
the rolling ledger file, and ``git diff -U0`` for what each open pull request and the new branch change.
A source it cannot read is ``DuplicateGuardUncheckedError`` with the reason: an unread watcher or ledger never
means no duplicate, and a lane that cannot be checked does not open a pull request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from functools import cache
from hashlib import sha1
from pathlib import Path
from typing import Final

import yaml

from omnimarket.handlers.scm.handler_scm_local_state import (
    WATCHER_SCHEMA,
    default_clone_path,
    default_watcher_state_path,
)
from omnimarket.models.model_pr_duplicate_guard import (
    ModelDuplicateDigestConfig,
    ModelDuplicateGuardRules,
    ModelFileTouch,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateSnapshotRequest,
    ModelPrDuplicateSnapshotResult,
    ModelPrTouch,
)

logger = logging.getLogger(__name__)

LEDGER_ENV: Final[str] = "ONEX_LEDGER_PATH"
#: Where a deployment's overlay for the guard is found: the roots every node overlay is read from, then the
#: workspace configuration root's overlays directory.
OVERLAY_ROOTS_ENV: Final[str] = "ONEX_SKILL_OVERLAY_ROOTS"
WORKSPACE_CONFIG_ENV: Final[str] = "ONEX_WORKSPACE_CONFIG_ROOT"
OVERLAY_NODE: Final[str] = "node_pr_duplicate_guard_compute"
OVERLAY_FILE: Final[str] = "overlay.yaml"
#: The watcher ticks every five minutes; a record this much older than now says the watcher is down.
WATCHER_MAX_AGE: Final[timedelta] = timedelta(minutes=15)
FETCH_TIMEOUT_S: Final[int] = 120
DIFF_TIMEOUT_S: Final[int] = 60
_GIT_LOCATION_ENV: Final[frozenset[str]] = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"}
)
_FILE_HEADER = re.compile(r"^diff --git a/(.+) b/\1$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]*$")


@cache
def load_digest_config() -> ModelDuplicateDigestConfig:
    """Fingerprint settings from this effect's own contract."""
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    return ModelDuplicateDigestConfig.model_validate(contract["config"]["digest"])


class DuplicateGuardUncheckedError(Exception):
    """The guard could not read a source it needs; the message says which and how to fix it."""


def _git(cwd: Path, *args: str, timeout: int = 30) -> str | None:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV}
    try:
        done = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s in %s failed: %s", args[0], cwd, exc)
        return None
    return done.stdout if done.returncode == 0 else None


def parse_diff(diff: str) -> ModelPrTouch:
    """Files, old-line spans and signed changed-line fingerprints, excluding diff headers."""
    config = load_digest_config()
    spans: dict[str, list[tuple[int, int]]] = {}
    digests: dict[str, set[str]] = {}
    added: set[str] = set()
    current: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            current = header[1]
            spans[current] = []
            digests[current] = set()
            in_hunk = False
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            added.add(current)
        else:
            hunk = _HUNK.match(line)
            if hunk:
                in_hunk = True
                count = 1 if hunk[2] is None else int(hunk[2])
                if count > 0:
                    spans[current].append((int(hunk[1]), int(hunk[1]) + count - 1))
            elif in_hunk and line.startswith(("-", "+")):
                text = line[1:].strip()
                if text and len(text) >= config.min_line_chars:
                    digests[current].add(
                        sha1(
                            (line[0] + text).encode(), usedforsecurity=False
                        ).hexdigest()[: config.prefix_chars]
                    )
    return ModelPrTouch(
        files=tuple(
            ModelFileTouch(
                path=path,
                added=path in added,
                spans=tuple(found),
                digest=tuple(sorted(digests[path])),
            )
            for path, found in sorted(spans.items())
        )
    )


def _resolves(repo_dir: Path, ref: str) -> bool:
    return (
        _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        is not None
    )


def _diff(repo_dir: Path, base_ref: str, head_ref: str) -> ModelPrTouch | None:
    text = _git(
        repo_dir,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        f"{base_ref}...{head_ref}",
        timeout=DIFF_TIMEOUT_S,
    )
    return parse_diff(text) if text is not None else None


def _base_ref(repo_dir: Path, base: str | None) -> str | None:
    candidates = [f"refs/remotes/origin/{base}", f"refs/heads/{base}"] if base else []
    head = _git(repo_dir, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head:
        candidates.append(head.strip())
    return next((c for c in candidates if _resolves(repo_dir, c)), None)


def _refresh_base(repo_dir: Path, base: str | None) -> None:
    if base and _BRANCH.match(base):
        _git(
            repo_dir,
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
            timeout=FETCH_TIMEOUT_S,
        )


def new_branch_touch(
    repo: str,
    head_ref: str,
    base: str | None,
    worktree: Path | None,
    clone: Path | None,
    diff_base: str | None = None,
) -> ModelPrTouch:
    """What the new branch changes against its base, read where the lane holds it: its worktree, else the clone."""
    if not _BRANCH.match(head_ref):
        raise DuplicateGuardUncheckedError(
            f"branch name {head_ref!r} is not a plain branch name"
        )
    for repo_dir in (worktree, clone):
        if repo_dir is None or not repo_dir.is_dir():
            continue
        head = next(
            (
                r
                for r in (f"refs/heads/{head_ref}", f"refs/remotes/origin/{head_ref}")
                if _resolves(repo_dir, r)
            ),
            None,
        )
        if head is None:
            continue
        _refresh_base(repo_dir, base)
        base_ref = diff_base or _base_ref(repo_dir, base)
        touch = _diff(repo_dir, base_ref, head) if base_ref else None
        if touch is not None:
            return touch
    raise DuplicateGuardUncheckedError(
        f"cannot read what branch {head_ref} changes in {repo}: no git checkout here holds the branch and its base "
        "(run from the lane's worktree, or push the branch first)"
    )


def _load_watcher(path: Path | None, now: datetime) -> dict[str, object]:
    if path is None or not path.is_file():
        raise DuplicateGuardUncheckedError(
            "no PR watcher state file: set ONEX_PR_WATCHER_STATE (or OMNI_HOME) so open pull requests can be read"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DuplicateGuardUncheckedError(
            f"PR watcher state {path} is unreadable: {exc}"
        ) from exc
    if (
        not isinstance(state, dict)
        or state.get("schema") != WATCHER_SCHEMA
        or not isinstance(state.get("prs"), dict)
    ):
        raise DuplicateGuardUncheckedError(
            f"PR watcher state {path} is not schema {WATCHER_SCHEMA}"
        )
    tick = state.get("last_tick")
    try:
        stamp = datetime.fromisoformat(str(tick).replace("Z", "+00:00"))
    except ValueError as exc:
        raise DuplicateGuardUncheckedError(
            f"PR watcher state {path} has no readable last_tick"
        ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    if now - stamp > WATCHER_MAX_AGE:
        raise DuplicateGuardUncheckedError(
            f"PR watcher state last_tick {tick} is older than {int(WATCHER_MAX_AGE.total_seconds() // 60)} minutes: "
            "the watcher is not ticking, so the open pull requests cannot be trusted"
        )
    return state


def _load_ledger(path: Path | None) -> str:
    if path is None:
        raise DuplicateGuardUncheckedError(
            f"{LEDGER_ENV} is not set; it names the rolling work ledger (rule 8)"
        )
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DuplicateGuardUncheckedError(
            f"the ledger {path} is unreadable: {exc}"
        ) from exc


def _open_prs(
    state: dict[str, object],
    repo: str,
    base: str | None,
    clone: Path | None,
    *,
    worktree: Path | None = None,
    new_head: str | None = None,
) -> tuple[ModelOpenPr, ...]:
    prs = state["prs"]
    assert isinstance(prs, dict)
    short = repo.split("/")[-1].casefold()
    picked: list[tuple[dict[str, object], str]] = []
    for record in prs.values():
        facts = record.get("facts") if isinstance(record, dict) else None
        if not isinstance(facts, dict) or facts.get("state") != "OPEN":
            continue
        if str(facts.get("repo", "")).split("/")[-1].casefold() != short:
            continue
        picked.append((facts, str(facts.get("head_sha") or "")))
    missing = [
        f"refs/pull/{facts['number']}/head"
        for facts, sha in picked
        if clone is not None and sha and not _resolves(clone, sha)
    ]
    if clone is not None and missing:
        _git(
            clone,
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            *missing,
            timeout=FETCH_TIMEOUT_S,
        )
    if clone is not None:
        for target in {
            str(f.get("base") or "")
            for f, _ in picked
            if base is None or f.get("base") == base
        }:
            _refresh_base(clone, target or None)
    out: list[ModelOpenPr] = []
    for facts, sha in sorted(picked, key=lambda p: int(str(p[0]["number"]))):
        pr_base = str(facts.get("base") or "")
        touch: ModelPrTouch | None = None
        wanted = base is None or pr_base == base
        if wanted and clone is not None and sha and _resolves(clone, sha):
            base_ref = _base_ref(clone, pr_base)
            touch = _diff(clone, base_ref, sha) if base_ref else None
        out.append(
            ModelOpenPr(
                number=int(str(facts["number"])),
                head_ref=str(facts.get("head_ref") or ""),
                base=pr_base,
                title=str(facts.get("title") or ""),
                body=str(facts.get("body") or ""),
                touch=touch,
                draft=bool(facts.get("draft", False)),
                stacked=bool(
                    new_head
                    and sha
                    and sha != new_head
                    and (
                        _is_ancestor((worktree, clone), sha, new_head)
                        or _is_ancestor((worktree, clone), new_head, sha)
                    )
                ),
            )
        )
    return tuple(out)


def _is_ancestor(dirs: tuple[Path | None, ...], ancestor: str, head: str) -> bool:
    """Try the lane's object database first, then the clone's."""
    return any(
        repo_dir is not None
        and _git(repo_dir, "merge-base", "--is-ancestor", ancestor, head) is not None
        for repo_dir in dirs
    )


def _new_head(head_ref: str, dirs: tuple[Path | None, ...]) -> str | None:
    if not _BRANCH.match(head_ref):
        return None
    for repo_dir in dirs:
        if repo_dir is None or not repo_dir.is_dir():
            continue
        for ref in (f"refs/heads/{head_ref}", f"refs/remotes/origin/{head_ref}"):
            sha = _git(
                repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"
            )
            if sha:
                return sha.strip()
    return None


def _stack_base(
    state: dict[str, object],
    repo: str,
    base: str | None,
    head: str | None,
    dirs: tuple[Path | None, ...],
) -> str | None:
    """Exclude the tip-most open ancestor's changes when the requested base does not name one.

    Incomparable ancestors of a merge are tied by SHA so the snapshot stays deterministic.
    """
    if head is None:
        return None
    prs = state["prs"]
    assert isinstance(prs, dict)
    ancestors: dict[str, str] = {}
    for record in prs.values():
        facts = record.get("facts") if isinstance(record, dict) else None
        if not isinstance(facts, dict) or facts.get("state") != "OPEN":
            continue
        if (
            str(facts.get("repo", "")).split("/")[-1].casefold()
            != repo.split("/")[-1].casefold()
        ):
            continue
        sha = str(facts.get("head_sha") or "")
        if sha and sha != head and _is_ancestor(dirs, sha, head):
            ancestors[str(facts.get("head_ref") or "")] = sha
    if base in ancestors:
        return None
    candidates = sorted(set(ancestors.values()))
    return next(
        (
            sha
            for sha in candidates
            if not any(
                other != sha and _is_ancestor(dirs, sha, other) for other in candidates
            )
        ),
        None,
    )


def _overlay_candidates() -> list[Path]:
    roots = [r for r in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if r]
    config_root = os.environ.get(WORKSPACE_CONFIG_ENV)
    if config_root:
        roots.append(str(Path(config_root) / "config" / "onex" / "overlays"))
    return [Path(root) / OVERLAY_NODE / OVERLAY_FILE for root in roots]


def load_generated_paths() -> dict[str, tuple[str, ...]]:
    """The deployment overlay's per-repository generated paths: the first overlay found, or none declared.

    An overlay that exists and is not exactly a ``generated_paths`` mapping is unchecked, never ignored: a
    guard that silently dropped its declarations would refuse every plan pull request again.
    """
    path = next((c for c in _overlay_candidates() if c.is_file()), None)
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"generated_paths"}:
            raise ValueError("overlay must contain exactly generated_paths")
        return ModelDuplicateGuardRules.model_validate(
            {"generated_paths": raw["generated_paths"]}
        ).generated_paths
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError) as exc:
        raise DuplicateGuardUncheckedError(
            f"the duplicate guard's deployment overlay {path} is invalid ({type(exc).__name__}): "
            "it must be a mapping with exactly generated_paths, repository to a list of paths"
        ) from None


def read_request(
    *,
    repo: str,
    head_ref: str,
    base: str | None,
    title: str,
    body: str,
    lane: str | None,
    cause: str | None,
    watcher_state: Path | None = None,
    ledger: Path | None = None,
    clone: Path | None = None,
    worktree: Path | None = None,
    now: datetime | None = None,
) -> ModelPrDuplicateGuardRequest:
    """The fold's request for one new pull request, or ``DuplicateGuardUncheckedError``."""
    moment = now or datetime.now(UTC)
    state = _load_watcher(watcher_state or default_watcher_state_path(), moment)
    declared = os.environ.get(LEDGER_ENV)
    ledger_text = _load_ledger(ledger or (Path(declared) if declared else None))
    where = clone or default_clone_path(repo)
    lane_dir = worktree or Path.cwd()
    dirs = (lane_dir, where)
    head = _new_head(head_ref, dirs)
    open_prs = _open_prs(state, repo, base, where, worktree=lane_dir, new_head=head)
    diff_base = _stack_base(state, repo, base, head, dirs)
    return ModelPrDuplicateGuardRequest(
        repo=repo,
        head_ref=head_ref,
        base=base,
        title=title,
        body=body,
        lane=lane,
        cause=cause,
        now=moment,
        touch=new_branch_touch(repo, head_ref, base, lane_dir, where, diff_base),
        open_prs=open_prs,
        ledger_text=ledger_text,
        generated_paths=load_generated_paths(),
    )


class HandlerPrDuplicateSnapshot:
    """Read the watcher state, the ledger and git for one pull request about to be opened."""

    def handle(
        self, request: ModelPrDuplicateSnapshotRequest
    ) -> ModelPrDuplicateSnapshotResult:
        try:
            read = read_request(
                repo=request.repo,
                head_ref=request.head_ref,
                base=request.base,
                title=request.title,
                body=request.body,
                lane=request.lane,
                cause=request.cause,
                watcher_state=Path(request.watcher_state)
                if request.watcher_state
                else None,
                ledger=Path(request.ledger) if request.ledger else None,
                clone=Path(request.clone) if request.clone else None,
                worktree=Path(request.worktree) if request.worktree else None,
            )
        except DuplicateGuardUncheckedError as exc:
            return ModelPrDuplicateSnapshotResult(unchecked_reason=str(exc))
        return ModelPrDuplicateSnapshotResult(request=read)
