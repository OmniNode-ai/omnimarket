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
from collections.abc import Iterable
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
from omnimarket.models.model_github_gateway_io import ModelOpenPrFactsResult
from omnimarket.models.model_pr_duplicate_guard import (
    EnumStackBaseRule,
    ModelDuplicateDigestConfig,
    ModelDuplicateGuardRules,
    ModelDuplicateStackConfig,
    ModelFileTouch,
    ModelJudgedCommit,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateSnapshotRequest,
    ModelPrDuplicateSnapshotResult,
    ModelPrTouch,
    ModelReplacedPrFacts,
    ModelSpanCommits,
    validate_replaces,
)
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.ports.protocol_open_pr_facts_source import (
    OpenPrFactsUnreadableError,
    ProtocolOpenPrFactsSource,
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
_HUNK_RANGES = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BLAME_LINE = re.compile(r"^([0-9a-f]{40}) \d+ \d+")
_SHA_PREFIX: Final[int] = 12
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


@cache
def load_stack_config() -> ModelDuplicateStackConfig:
    """The stack rule from this effect's own contract."""
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    return ModelDuplicateStackConfig.model_validate(contract["config"]["stack"])


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


def _branch_tip(repo_dir: Path, head_ref: str) -> str | None:
    """The branch's newest tip in ``repo_dir``: its local branch or its pushed one, whichever descends from the other.

    A checkout whose local branch is behind the pushed one (the canonical clone's was, on 2026-11-01) is read at
    the pushed tip; unpushed commits of a local branch ahead of it still win. Diverged: the local branch.
    """
    tips: list[str] = []
    for ref in (f"refs/heads/{head_ref}", f"refs/remotes/origin/{head_ref}"):
        sha = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha and sha.strip() not in tips:
            tips.append(sha.strip())
    if not tips:
        return None
    best = tips[0]
    for tip in tips[1:]:
        if _is_ancestor((repo_dir,), best, tip):
            best = tip
    return best


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
        head = _branch_tip(repo_dir, head_ref)
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


def _attribute(
    repo_dir: Path,
    base_ref: str,
    sha: str,
    touch: ModelPrTouch,
    new_touch: ModelPrTouch,
) -> ModelPrTouch:
    """``touch`` with, for each hunk the new branch also changes, the commits that introduced its lines.

    Only hunks whose pre-image lines overlap one of the new branch's own spans are read, so a 200-commit bundle
    costs one blame per overlap. A hunk that only deletes lines has no lines to blame and stays unattributed;
    so does one whose blame fails: an overlap nobody can attribute is counted, never set aside.
    """
    mine = {f.path: f for f in new_touch.files}
    files: list[ModelFileTouch] = []
    for theirs in touch.files:
        other = mine.get(theirs.path)
        if other is None or not other.spans or not theirs.spans:
            files.append(theirs)
            continue
        text = _git(
            repo_dir,
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            f"{base_ref}...{sha}",
            "--",
            theirs.path,
            timeout=DIFF_TIMEOUT_S,
        )
        parts: list[ModelSpanCommits] = []
        for line in (text or "").splitlines():
            hunk = _HUNK_RANGES.match(line)
            if hunk is None:
                continue
            old_start = int(hunk[1])
            old_count = 1 if hunk[2] is None else int(hunk[2])
            new_start = int(hunk[3])
            new_count = 1 if hunk[4] is None else int(hunk[4])
            if old_count == 0:
                continue
            old = (old_start, old_start + old_count - 1)
            if not any(a[0] <= old[1] and old[0] <= a[1] for a in other.spans):
                continue
            commits = (
                _blame(repo_dir, sha, theirs.path, new_start, new_start + new_count - 1)
                if new_count > 0
                else ()
            )
            parts.append(ModelSpanCommits(start=old[0], end=old[1], commits=commits))
        files.append(theirs.model_copy(update={"attributed": tuple(parts)}))
    return ModelPrTouch(files=tuple(files))


def _blame(
    repo_dir: Path, sha: str, path: str, first: int, last: int
) -> tuple[str, ...]:
    """The 12-character shas that last changed lines ``first``..``last`` of ``path`` at ``sha``; empty when unread."""
    text = _git(
        repo_dir,
        "blame",
        "--porcelain",
        "-L",
        f"{first},{last}",
        sha,
        "--",
        path,
        timeout=DIFF_TIMEOUT_S,
    )
    if text is None:
        return ()
    found: list[str] = []
    for line in text.splitlines():
        match = _BLAME_LINE.match(line)
        if match and match[1][:_SHA_PREFIX] not in found:
            found.append(match[1][:_SHA_PREFIX])
    return tuple(found)


def _replaced_facts(
    state: dict[str, object],
    replaces: tuple[str, ...],
    repo: str,
    clone: Path | None,
) -> tuple[ModelReplacedPrFacts, ...]:
    """What the node reads of each ``replaces`` entry: its state from the watcher state, its commits from git.

    The caller's word is never the source: the entry is looked up in the same state the open pull requests come
    from, and a pull request that state does not hold is ``state=None`` (the guard refuses it as unreadable).
    """
    prs = state["prs"]
    assert isinstance(prs, dict)
    out: list[ModelReplacedPrFacts] = []
    for item in validate_replaces(replaces):
        name, _, number_text = item.partition("#")
        number = int(number_text)
        if name.casefold() != repo.casefold():
            out.append(ModelReplacedPrFacts(repo=name, number=number))
            continue
        record = prs.get(f"{name.split('/')[-1]}#{number}")
        facts = record.get("facts") if isinstance(record, dict) else None
        state_name = facts.get("state") if isinstance(facts, dict) else None
        if not isinstance(facts, dict) or state_name not in (
            "OPEN",
            "CLOSED",
            "MERGED",
        ):
            out.append(ModelReplacedPrFacts(repo=name, number=number))
            continue
        merged = state_name == "MERGED" or bool(facts.get("merged_at"))
        commits: tuple[str, ...] = ()
        if state_name == "CLOSED" and not merged and clone is not None:
            commits = _own_commits(
                clone,
                str(facts.get("head_sha") or ""),
                number,
                str(facts.get("base") or ""),
            )
        out.append(
            ModelReplacedPrFacts(
                repo=name,
                number=number,
                state=state_name,
                merged=merged,
                commits=commits,
            )
        )
    return tuple(out)


def _own_commits(clone: Path, sha: str, number: int, base: str) -> tuple[str, ...]:
    """The 12-character shas of a pull request's own commits: its head past its base branch, newest first."""
    if not sha:
        return ()
    if not _resolves(clone, sha):
        _git(
            clone,
            "fetch",
            "--no-tags",
            "--quiet",
            "origin",
            f"refs/pull/{number}/head",
            timeout=FETCH_TIMEOUT_S,
        )
    base_ref = _base_ref(clone, base or None)
    if base_ref is None or not _resolves(clone, sha):
        return ()
    text = _git(clone, "rev-list", "--no-color", sha, f"^{base_ref}")
    return tuple(line[:_SHA_PREFIX] for line in (text or "").split() if line)


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


def _state_from_gateway(
    repo: str, source: ProtocolOpenPrFactsSource, now: datetime
) -> dict[str, object]:
    """The open pull requests of ``repo`` read live from the gateway, in the watcher state's own shape.

    The fold reads the same records whichever source gave them. The list is complete or it is an error:
    a cut or a mismatched answer never stands for "no other pull request is open" (OMN-22654).
    """
    result: ModelOpenPrFactsResult = source.read(repo)
    if result.repo.casefold() != repo.casefold():
        raise OpenPrFactsUnreadableError(f"it answered for {result.repo}, not {repo}")
    if result.truncated:
        total = result.total_count if result.total_count is not None else "more"
        raise OpenPrFactsUnreadableError(
            f"it was cut at {len(result.prs)} of {total} open pull requests, so it cannot say "
            "that none of the rest duplicates this one"
        )
    short = repo.split("/")[-1]
    return {
        "schema": WATCHER_SCHEMA,
        "last_tick": now.isoformat(),
        "prs": {
            f"{short}#{pr.number}": {
                "facts": {
                    "state": "OPEN",
                    "repo": short,
                    "number": pr.number,
                    "head_ref": pr.head_ref,
                    "head_sha": pr.head_sha,
                    "base": pr.base_ref,
                    "title": pr.title,
                    "body": "\n".join(pr.fix_markers),
                    "draft": pr.draft,
                }
            }
            for pr in result.prs
        },
    }


def _open_pr_state(
    repo: str,
    watcher_state: Path | None,
    now: datetime,
    open_prs_source: ProtocolOpenPrFactsSource | None,
) -> dict[str, object]:
    """The open pull requests: the PR watcher's fresh state, else a live gateway read, else unchecked.

    A host that has no watcher state (a lab host) or whose watcher is not ticking reads the gateway. When
    that read is not possible either, the guard is unchecked and the reason names both reads it lacks.
    """
    try:
        return _load_watcher(watcher_state or default_watcher_state_path(), now)
    except DuplicateGuardUncheckedError as watcher:
        if open_prs_source is None:
            raise
        try:
            return _state_from_gateway(repo, open_prs_source, now)
        except OpenPrFactsUnreadableError as gateway:
            raise DuplicateGuardUncheckedError(
                f"{watcher}; and the gateway read of the open pull requests of {repo} "
                f"(open_pr_facts) failed: {gateway}"
            ) from None


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
    attribute_against: ModelPrTouch | None = None,
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
            if touch is not None and base_ref and attribute_against is not None:
                touch = _attribute(clone, base_ref, sha, touch, attribute_against)
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
                    and any(
                        tip != new_head
                        and (
                            _is_ancestor((worktree, clone), tip, new_head)
                            or _is_ancestor((worktree, clone), new_head, tip)
                        )
                        for tip in _tips(
                            (worktree, clone), sha, str(facts.get("head_ref") or "")
                        )
                    )
                ),
            )
        )
    return tuple(out)


def _tips(dirs: tuple[Path | None, ...], sha: str, head_ref: str) -> tuple[str, ...]:
    """A pull request's head as the watcher recorded it, and its branch's tip as the lane's repository holds it now.

    The watcher ticks every five minutes, so a push since then moves the branch past the recorded sha.
    """
    found = [sha] if sha else []
    if _BRANCH.match(head_ref):
        for repo_dir in dirs:
            if repo_dir is None or not repo_dir.is_dir():
                continue
            tip = _git(
                repo_dir,
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/remotes/origin/{head_ref}^{{commit}}",
            )
            if tip and tip.strip() not in found:
                found.append(tip.strip())
    return tuple(found)


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
        tip = _branch_tip(repo_dir, head_ref)
        if tip:
            return tip
    return None


def _contained_tips(
    state: dict[str, object],
    repo: str,
    head: str,
    dirs: tuple[Path | None, ...],
) -> dict[str, str]:
    """The open pull requests the branch contains: each one's branch name to the tip of it that is an ancestor of ``head``."""
    prs = state["prs"]
    assert isinstance(prs, dict)
    contained: dict[str, str] = {}
    for record in prs.values():
        facts = record.get("facts") if isinstance(record, dict) else None
        if not isinstance(facts, dict) or facts.get("state") != "OPEN":
            continue
        if (
            str(facts.get("repo", "")).split("/")[-1].casefold()
            != repo.split("/")[-1].casefold()
        ):
            continue
        for sha in _tips(
            dirs, str(facts.get("head_sha") or ""), str(facts.get("head_ref") or "")
        ):
            if sha != head and _is_ancestor(dirs, sha, head):
                contained[str(facts.get("head_ref") or "")] = sha
    return contained


def _commit_time(dirs: tuple[Path | None, ...], sha: str) -> int:
    for repo_dir in dirs:
        if repo_dir is None or not repo_dir.is_dir():
            continue
        stamp = _git(repo_dir, "log", "-1", "--format=%ct", sha)
        if stamp and stamp.strip().isdigit():
            return int(stamp)
    return 0


def _stack_base(
    contained: dict[str, str],
    base: str | None,
    dirs: tuple[Path | None, ...],
) -> str | None:
    """Where the branch's own change starts, by the contract's stack rule.

    ``NEWEST_CONTAINED_OPEN_PR_HEAD``: the newest head among the contained open pull requests no other contained
    one descends from (ties by SHA, so the snapshot stays deterministic); None when the requested base already
    names one of them, or the branch contains none. ``BASE_BRANCH``: None always.
    """
    if (
        load_stack_config().base_rule
        is not EnumStackBaseRule.NEWEST_CONTAINED_OPEN_PR_HEAD
    ):
        return None
    if base in contained:
        return None
    candidates = sorted(set(contained.values()))
    maximal = [
        sha
        for sha in candidates
        if not any(
            other != sha and _is_ancestor(dirs, sha, other) for other in candidates
        )
    ]
    return max(maximal, key=lambda sha: (_commit_time(dirs, sha), sha), default=None)


def judged_commits(
    dirs: tuple[Path | None, ...],
    head: str | None,
    base: str | None,
    diff_base: str | None,
    contained: Iterable[str],
) -> tuple[ModelJudgedCommit, ...]:
    """The commits the guard judges: reachable from ``head``, newest first, past the base branch and every contained open pull request.

    Empty when no checkout holds both the head and a base to count from (the diff then was not read either).
    """
    if head is None:
        return ()
    for repo_dir in dirs:
        if repo_dir is None or not repo_dir.is_dir() or not _resolves(repo_dir, head):
            continue
        base_ref = _base_ref(repo_dir, base)
        if base_ref is None and diff_base is None:
            continue
        excluded = [*(r for r in (base_ref, diff_base) if r), *contained]
        text = _git(
            repo_dir,
            "log",
            "--no-color",
            "--format=%H%x09%s",
            head,
            *(f"^{ref}" for ref in dict.fromkeys(excluded) if _resolves(repo_dir, ref)),
        )
        if text is None:
            continue
        return tuple(
            ModelJudgedCommit(sha=sha[:12], subject=subject)
            for sha, _, subject in (line.partition("\t") for line in text.splitlines())
            if sha
        )
    return ()


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
    open_prs_source: ProtocolOpenPrFactsSource | None = None,
    replaces: tuple[str, ...] = (),
) -> ModelPrDuplicateGuardRequest:
    """The fold's request for one new pull request, or ``DuplicateGuardUncheckedError``."""
    moment = now or datetime.now(UTC)
    state = _open_pr_state(repo, watcher_state, moment, open_prs_source)
    declared = os.environ.get(LEDGER_ENV)
    ledger_text = _load_ledger(ledger or (Path(declared) if declared else None))
    where = clone or default_clone_path(repo)
    lane_dir = worktree or Path.cwd()
    dirs = (lane_dir, where)
    head = _new_head(head_ref, dirs)
    contained = _contained_tips(state, repo, head, dirs) if head else {}
    diff_base = _stack_base(contained, base, dirs)
    # Only a caller that names a replaced pull request pays for reading the new branch's diff first and blaming
    # the overlaps; without ``replaces`` the reads and their order are exactly what they were.
    early_touch = (
        new_branch_touch(repo, head_ref, base, lane_dir, where, diff_base)
        if replaces
        else None
    )
    open_prs = _open_prs(
        state,
        repo,
        base,
        where,
        worktree=lane_dir,
        new_head=head,
        attribute_against=early_touch,
    )
    return ModelPrDuplicateGuardRequest(
        repo=repo,
        head_ref=head_ref,
        base=base,
        title=title,
        body=body,
        lane=lane,
        cause=cause,
        now=moment,
        touch=early_touch
        or new_branch_touch(repo, head_ref, base, lane_dir, where, diff_base),
        judged_commits=judged_commits(dirs, head, base, diff_base, contained.values()),
        judged_base=diff_base[:12] if diff_base else None,
        open_prs=open_prs,
        ledger_text=ledger_text,
        generated_paths=load_generated_paths(),
        replaced=_replaced_facts(state, replaces, repo, where) if replaces else (),
    )


class HandlerPrDuplicateSnapshot:
    """Read the watcher state (or the gateway), the ledger and git for one pull request about to be opened."""

    def __init__(
        self, open_prs_source: ProtocolOpenPrFactsSource | None = None
    ) -> None:
        self._open_prs_source = open_prs_source

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
                open_prs_source=self._open_prs_source,
                replaces=request.replaces,
            )
        except DuplicateGuardUncheckedError as exc:
            return ModelPrDuplicateSnapshotResult(unchecked_reason=str(exc))
        return ModelPrDuplicateSnapshotResult(request=read)
