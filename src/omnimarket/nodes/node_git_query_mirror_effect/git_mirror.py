# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The git side of node_git_query_mirror_effect (OMN-19617).

A fetch-only bare mirror per repository answers the PR reads that git can
answer (head sha, changed files, diff, merge-tree conflicts, whether the change
is already on the base branch, the commit log) without spending GitHub API
quota: ``git fetch`` is the git transport, not the REST or GraphQL API.

Why a dedicated mirror and not the canonical clones
--------------------------------------------------
Fetching PR heads into a canonical clone puts them under ``refs/remotes/origin``
where (1) every lane sees them in ``git branch -r`` and (2) any plain
``git fetch --prune`` by any lane deletes all of them, because the default
refspec's destination covers ``refs/remotes/origin/*``. The mirror keeps its
own ``refs/heads/*`` and ``refs/pull/*/head`` namespaces, prunes only itself,
and never writes into a canonical clone. A canonical clone is used, at most, as
a ``--reference`` for the first clone, and ``--dissociate`` copies the objects
so the mirror never depends on the canonical clone's object store.

Freshness
---------
A full ``sync`` writes ``onex_query_watermark.json`` into the mirror. A query
whose mirror is older than its ``max_age_s`` (or lacks the PR ref) first
fetches just that PR ref and the base branch; only when that git fetch fails
does it fall back to the GitHub API, and the answer says so in ``source``.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    MAX_DIFF_CHARS,
    PR_SCOPED_OPERATIONS,
    EnumGitQueryOperation,
    EnumGitQuerySource,
    EnumOnBaseReason,
    ModelGitCommit,
    ModelGitQueryRequest,
    ModelGitQueryResult,
)

WATERMARK_FILE = "onex_query_watermark.json"
HEADS_REFSPEC = "+refs/heads/*:refs/heads/*"
PULLS_REFSPEC = "+refs/pull/*/head:refs/pull/*/head"
FETCH_TIMEOUT_S = 600
QUERY_TIMEOUT_S = 120
LOCK_WAIT_S = 300

# The registry repositories a scheduled sync keeps current when it is given
# no explicit list. Adding a repo here is the whole change needed to mirror it.
REGISTRY_REPOS: tuple[str, ...] = (
    "OmniNode-ai/omnibase_compat",
    "OmniNode-ai/omnibase_core",
    "OmniNode-ai/omnibase_spi",
    "OmniNode-ai/omnibase_infra",
    "OmniNode-ai/omnimarket",
    "OmniNode-ai/omniclaude",
    "OmniNode-ai/omniintelligence",
    "OmniNode-ai/omnimemory",
    "OmniNode-ai/omnidash",
    "OmniNode-ai/onex_change_control",
    "OmniNode-ai/omninode_infra",
    "OmniNode-ai/knowledge-base-internal",
    "OmniNode-ai/omniclaude-internal",
    "OmniNode-ai/omni_home",
)

GhRunner = Callable[[list[str]], tuple[int, str, str]]


def resolve_mirror_root() -> Path:
    """The mirror root: ONEX_GIT_QUERY_MIRROR_ROOT, else $ONEX_STATE_DIR/git-query-mirrors.

    Raises KeyError when neither is set (fail fast, never a guessed default).
    """
    explicit = os.environ.get("ONEX_GIT_QUERY_MIRROR_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.environ["ONEX_STATE_DIR"]) / "git-query-mirrors"


def canonical_clone_for(repo: str) -> Path | None:
    """The canonical clone under $OMNI_HOME for ``owner/name``, when one exists."""
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        return None
    name = repo.split("/", 1)[1]
    candidate = Path(omni_home) if name == "omni_home" else Path(omni_home) / name
    if (candidate / ".git").exists():
        return candidate
    return None


def default_remote_url(repo: str) -> str:
    """The canonical clone's origin URL (same auth path as this host already uses), else https."""
    clone = canonical_clone_for(repo)
    if clone is not None:
        code, out, _ = _git(clone, "remote", "get-url", "origin")
        if code == 0 and repo.split("/", 1)[1] in out:
            return out.strip()
    return f"https://github.com/{repo}.git"


def default_gh_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run gh; a missing gh is an ordinary failure, not a crash."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=QUERY_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        return 127, "", "gh not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "gh timed out"
    return proc.returncode, proc.stdout, proc.stderr


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    where: Path, *args: str, timeout: int = QUERY_TIMEOUT_S
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(where), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_git_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def _tail(text: str, n: int = 300) -> str:
    text = text.strip()
    return text[-n:]


class GitQueryMirror:
    """A root directory of per-repo bare mirrors, and the queries over them."""

    def __init__(
        self,
        root: Path,
        remote_url_for: Callable[[str], str] = default_remote_url,
        gh_runner: GhRunner = default_gh_runner,
        reference_clone_for: Callable[[str], Path | None] = canonical_clone_for,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.remote_url_for = remote_url_for
        self.gh_runner = gh_runner
        self.reference_clone_for = reference_clone_for
        self.clock = clock

    # -- paths and bookkeeping -------------------------------------------------

    def mirror_path(self, repo: str) -> Path:
        owner, name = repo.split("/", 1)
        return self.root / owner / f"{name}.git"

    def _watermark(self, mirror: Path) -> dict[str, object] | None:
        try:
            data = json.loads((mirror / WATERMARK_FILE).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _age(self, mirror: Path) -> float | None:
        wm = self._watermark(mirror)
        epoch = wm.get("fetched_at_epoch") if wm else None
        if not isinstance(epoch, (int, float)):
            return None
        return max(0.0, self.clock() - float(epoch))

    def _count(self, mirror: Path, prefix: str) -> int:
        code, out, _ = _git(mirror, "for-each-ref", "--format=%(refname)", prefix)
        return len(out.splitlines()) if code == 0 else 0

    @contextmanager
    def _lock(self, mirror: Path) -> Iterator[None]:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        lock_path = mirror.parent / f".{mirror.name}.lock"
        with lock_path.open("a") as fh:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"mirror lock {lock_path} held for {LOCK_WAIT_S}s"
                        ) from exc
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # -- sync ------------------------------------------------------------------

    def _create(self, repo: str, mirror: Path) -> str | None:
        """Clone a bare mirror; returns an error string or None."""
        url = self.remote_url_for(repo)
        tmp = Path(tempfile.mkdtemp(prefix=f".{mirror.name}.", dir=mirror.parent))
        target = tmp / "m.git"
        args = ["clone", "--bare", "--no-tags", "--quiet"]
        ref = self.reference_clone_for(repo)
        if ref is not None:
            args += ["--reference-if-able", str(ref), "--dissociate"]
        code, _, err = _git(
            mirror.parent, *args, url, str(target), timeout=FETCH_TIMEOUT_S
        )
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return f"clone {url} failed: {_tail(err)}"
        for cfg in (
            ["config", "--unset-all", "remote.origin.fetch"],
            ["config", "--add", "remote.origin.fetch", HEADS_REFSPEC],
            ["config", "--add", "remote.origin.fetch", PULLS_REFSPEC],
            ["config", "remote.origin.tagOpt", "--no-tags"],
        ):
            code, _, err = _git(target, *cfg)
            if code not in (0, 5):  # 5: --unset-all found nothing to unset
                shutil.rmtree(tmp, ignore_errors=True)
                return f"git {' '.join(cfg)} failed: {_tail(err)}"
        target.rename(mirror)
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    def sync(self, repo: str) -> ModelGitQueryResult:
        """Create the mirror if needed, then fetch every head and PR ref (fetch only)."""
        mirror = self.mirror_path(repo)
        try:
            with self._lock(mirror):
                if not (mirror / "HEAD").exists():
                    err = self._create(repo, mirror)
                    if err:
                        return self._result_err(
                            EnumGitQueryOperation.SYNC, repo, None, mirror, err
                        )
                code, _, err = _git(
                    mirror,
                    "fetch",
                    "--prune",
                    "--no-tags",
                    "--quiet",
                    "origin",
                    timeout=FETCH_TIMEOUT_S,
                )
                prior = self._watermark(mirror) or {}
                now = self.clock()
                if code != 0:
                    prior.update(
                        {
                            "repo": repo,
                            "ok": False,
                            "last_error": _tail(err),
                            "last_attempt_at": _iso(now),
                        }
                    )
                    (mirror / WATERMARK_FILE).write_text(json.dumps(prior, indent=2))
                    return self._result_err(
                        EnumGitQueryOperation.SYNC,
                        repo,
                        None,
                        mirror,
                        f"fetch failed: {_tail(err)}",
                    )
                pulls = self._count(mirror, "refs/pull")
                heads = self._count(mirror, "refs/heads")
                wm = {
                    "repo": repo,
                    "ok": True,
                    "fetched_at": _iso(now),
                    "fetched_at_epoch": now,
                    "pull_refs": pulls,
                    "head_refs": heads,
                    "last_attempt_at": _iso(now),
                }
                (mirror / WATERMARK_FILE).write_text(json.dumps(wm, indent=2))
        except TimeoutError as exc:
            return self._result_err(
                EnumGitQueryOperation.SYNC, repo, None, mirror, str(exc)
            )
        return ModelGitQueryResult(
            operation=EnumGitQueryOperation.SYNC,
            repo=repo,
            ok=True,
            source=EnumGitQuerySource.MIRROR_REFRESHED,
            watermark_age_s=0.0,
            mirror_path=str(mirror),
            pull_refs=pulls,
            head_refs=heads,
        )

    def _refresh_pr(self, mirror: Path, pr: int, base: str) -> str | None:
        """Fetch one PR ref and the base branch. Returns an error string or None."""
        try:
            with self._lock(mirror):
                code, _, err = _git(
                    mirror,
                    "fetch",
                    "--no-tags",
                    "--quiet",
                    "origin",
                    f"+refs/pull/{pr}/head:refs/pull/{pr}/head",
                    f"+refs/heads/{base}:refs/heads/{base}",
                    timeout=FETCH_TIMEOUT_S,
                )
        except TimeoutError as exc:
            return str(exc)
        return None if code == 0 else f"targeted fetch failed: {_tail(err)}"

    # -- queries ---------------------------------------------------------------

    def query(self, request: ModelGitQueryRequest) -> ModelGitQueryResult:
        op = request.operation
        if op is EnumGitQueryOperation.SYNC:
            return self.sync(request.repo)
        mirror = self.mirror_path(request.repo)
        if op is EnumGitQueryOperation.STATUS:
            return self._status(request.repo, mirror)
        if op not in PR_SCOPED_OPERATIONS or request.pr_number is None:
            raise ValueError(f"operation {op.value} needs pr_number")
        pr = request.pr_number

        source = EnumGitQuerySource.MIRROR
        problem: str | None = None
        base = request.base or ""
        if not (mirror / "HEAD").exists():
            built = self.sync(request.repo)
            source = EnumGitQuerySource.MIRROR_REFRESHED
            problem = None if built.ok else built.error
        if problem is None:
            base = base or self._default_branch(mirror)
            age = self._age(mirror)
            has_ref = (
                _git(
                    mirror, "rev-parse", "--verify", "--quiet", f"refs/pull/{pr}/head"
                )[0]
                == 0
            )
            if age is None or age > request.max_age_s or not has_ref:
                problem = self._refresh_pr(mirror, pr, base)
                source = EnumGitQuerySource.MIRROR_REFRESHED
        if problem is not None:
            return self._fallback(request, mirror, problem)
        return self._answer(request, mirror, source, base)

    def _default_branch(self, mirror: Path) -> str:
        code, out, _ = _git(mirror, "symbolic-ref", "--short", "HEAD")
        return out.strip() if code == 0 and out.strip() else "main"

    def _status(self, repo: str, mirror: Path) -> ModelGitQueryResult:
        if not (mirror / "HEAD").exists():
            return self._result_err(
                EnumGitQueryOperation.STATUS, repo, None, mirror, "no mirror yet"
            )
        wm = self._watermark(mirror) or {}
        return ModelGitQueryResult(
            operation=EnumGitQueryOperation.STATUS,
            repo=repo,
            ok=bool(wm.get("ok")),
            source=EnumGitQuerySource.MIRROR,
            watermark_age_s=self._age(mirror),
            mirror_path=str(mirror),
            base_ref=self._default_branch(mirror),
            pull_refs=self._count(mirror, "refs/pull"),
            head_refs=self._count(mirror, "refs/heads"),
            error=str(wm["last_error"])
            if not wm.get("ok") and wm.get("last_error")
            else None,
        )

    def _answer(
        self,
        request: ModelGitQueryRequest,
        mirror: Path,
        source: EnumGitQuerySource,
        base: str,
    ) -> ModelGitQueryResult:
        op, pr = request.operation, request.pr_number
        common: dict[str, object] = {
            "operation": op,
            "repo": request.repo,
            "pr_number": pr,
            "source": source,
            "watermark_age_s": self._age(mirror),
            "mirror_path": str(mirror),
            "base_ref": base,
        }
        code, head, err = _git(
            mirror, "rev-parse", "--verify", "--quiet", f"refs/pull/{pr}/head"
        )
        if code != 0:
            return ModelGitQueryResult(
                **common, ok=False, error=f"no refs/pull/{pr}/head"
            )
        code, base_sha, err = _git(
            mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}"
        )
        if code != 0:
            return ModelGitQueryResult(
                **common, ok=False, error=f"no base branch {base}"
            )
        head, base_sha = head.strip(), base_sha.strip()
        common.update(head_sha=head, base_sha=base_sha)

        if op is EnumGitQueryOperation.HEAD_SHA:
            return ModelGitQueryResult(**common, ok=True)
        if op is EnumGitQueryOperation.CHANGED_FILES:
            code, out, err = _git(
                mirror, "diff", "--name-only", "--no-renames", f"{base_sha}...{head}"
            )
            if code != 0:
                return ModelGitQueryResult(**common, ok=False, error=_tail(err))
            mb = _git(mirror, "merge-base", base_sha, head)[1].strip() or None
            return ModelGitQueryResult(
                **common, ok=True, files=out.splitlines(), merge_base=mb
            )
        if op is EnumGitQueryOperation.DIFF:
            code, out, err = _git(mirror, "diff", f"{base_sha}...{head}")
            if code != 0:
                return ModelGitQueryResult(**common, ok=False, error=_tail(err))
            truncated = len(out) > MAX_DIFF_CHARS
            return ModelGitQueryResult(
                **common, ok=True, diff=out[:MAX_DIFF_CHARS], diff_truncated=truncated
            )
        if op is EnumGitQueryOperation.CONFLICTS:
            merged = self._merge_tree(mirror, base_sha, head)
            if isinstance(merged, str):
                return ModelGitQueryResult(**common, ok=False, error=merged)
            _tree, conflicted = merged
            return ModelGitQueryResult(
                **common,
                ok=True,
                conflicts=bool(conflicted),
                conflicted_files=conflicted,
            )
        if op is EnumGitQueryOperation.ON_BASE:
            if _git(mirror, "merge-base", "--is-ancestor", head, base_sha)[0] == 0:
                return ModelGitQueryResult(
                    **common,
                    ok=True,
                    on_base=True,
                    on_base_reason=EnumOnBaseReason.HEAD_IS_ANCESTOR,
                )
            merged = self._merge_tree(mirror, base_sha, head)
            if isinstance(merged, str):
                return ModelGitQueryResult(**common, ok=False, error=merged)
            tree, conflicted = merged
            if conflicted:
                return ModelGitQueryResult(
                    **common,
                    ok=True,
                    on_base=False,
                    conflicts=True,
                    conflicted_files=conflicted,
                    on_base_reason=EnumOnBaseReason.CONFLICTS,
                )
            base_tree = _git(mirror, "rev-parse", f"{base_sha}^{{tree}}")[1].strip()
            same = tree == base_tree
            return ModelGitQueryResult(
                **common,
                ok=True,
                on_base=same,
                conflicts=False,
                conflicted_files=[],
                on_base_reason=(
                    EnumOnBaseReason.MERGE_ADDS_NOTHING
                    if same
                    else EnumOnBaseReason.MERGE_ADDS_CHANGES
                ),
            )
        # LOG
        code, out, err = _git(
            mirror,
            "log",
            "--reverse",
            "--format=%H%x1f%s%x1f%an%x1f%aI",
            f"{base_sha}..{head}",
        )
        if code != 0:
            return ModelGitQueryResult(**common, ok=False, error=_tail(err))
        commits = []
        for line in out.splitlines():
            sha, subject, author, at = [*line.split("\x1f"), "", "", ""][:4]
            commits.append(
                ModelGitCommit(sha=sha, subject=subject, author=author, authored_at=at)
            )
        return ModelGitQueryResult(**common, ok=True, commits=commits)

    def _merge_tree(
        self, mirror: Path, base: str, head: str
    ) -> tuple[str, list[str]] | str:
        """(result tree, conflicted paths) from git merge-tree, or an error string."""
        code, out, err = _git(
            mirror,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            base,
            head,
        )
        if code not in (0, 1):
            return f"merge-tree failed: {_tail(err or out)}"
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return "merge-tree printed nothing"
        conflicted = sorted(set(lines[1:])) if code == 1 else []
        return lines[0].strip(), conflicted

    # -- fallback --------------------------------------------------------------

    def _fallback(
        self, request: ModelGitQueryRequest, mirror: Path, problem: str
    ) -> ModelGitQueryResult:
        op, pr, repo = request.operation, request.pr_number, request.repo
        base_fields: dict[str, object] = {
            "operation": op,
            "repo": repo,
            "pr_number": pr,
            "watermark_age_s": self._age(mirror),
            "mirror_path": str(mirror),
        }
        argv: list[str] | None = None
        if op is EnumGitQueryOperation.HEAD_SHA:
            argv = ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"]
        elif op is EnumGitQueryOperation.CHANGED_FILES:
            argv = [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/pulls/{pr}/files",
                "--jq",
                ".[].filename",
            ]
        elif op is EnumGitQueryOperation.DIFF:
            argv = ["gh", "pr", "diff", str(pr), "-R", repo]
        if argv is None or not request.allow_gh_fallback:
            why = (
                "no GitHub API fallback for this operation"
                if argv is None
                else "gh fallback disabled"
            )
            return ModelGitQueryResult(
                **base_fields,
                ok=False,
                source=EnumGitQuerySource.UNAVAILABLE,
                error=f"{problem}; {why}",
            )
        code, out, err = self.gh_runner(argv)
        if code != 0:
            return ModelGitQueryResult(
                **base_fields,
                ok=False,
                source=EnumGitQuerySource.UNAVAILABLE,
                error=f"{problem}; gh fallback failed: {_tail(err)}",
            )
        src = EnumGitQuerySource.GH_FALLBACK
        if op is EnumGitQueryOperation.HEAD_SHA:
            return ModelGitQueryResult(
                **base_fields, ok=True, source=src, head_sha=out.strip(), error=problem
            )
        if op is EnumGitQueryOperation.CHANGED_FILES:
            files = [f for f in out.splitlines() if f.strip()]
            return ModelGitQueryResult(
                **base_fields, ok=True, source=src, files=files, error=problem
            )
        return ModelGitQueryResult(
            **base_fields,
            ok=True,
            source=src,
            diff=out[:MAX_DIFF_CHARS],
            diff_truncated=len(out) > MAX_DIFF_CHARS,
            error=problem,
        )

    def _result_err(
        self,
        op: EnumGitQueryOperation,
        repo: str,
        pr: int | None,
        mirror: Path,
        error: str | None,
    ) -> ModelGitQueryResult:
        return ModelGitQueryResult(
            operation=op,
            repo=repo,
            pr_number=pr,
            ok=False,
            source=EnumGitQuerySource.UNAVAILABLE,
            watermark_age_s=self._age(mirror) if (mirror / "HEAD").exists() else None,
            mirror_path=str(mirror),
            error=error or "unknown error",
        )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "REGISTRY_REPOS",
    "GitQueryMirror",
    "canonical_clone_for",
    "default_gh_runner",
    "default_remote_url",
    "resolve_mirror_root",
]
