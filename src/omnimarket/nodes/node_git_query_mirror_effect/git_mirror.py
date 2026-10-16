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
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.events.github import pr_merged_event_id
from omnimarket.nodes.node_event_emit_effect import (
    EnumDurabilityTier,
    SpoolFile,
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    MAX_DIFF_CHARS,
    PR_SCOPED_OPERATIONS,
    EnumGitQueryOperation,
    EnumGitQuerySource,
    EnumOnBaseReason,
    ModelGitCommit,
    ModelGitQueryRequest,
    ModelGitQueryResult,
    ModelMirrorPrMerged,
)

WATERMARK_FILE = "onex_query_watermark.json"
PUBLISHED_MERGES_FILE = "onex_published_merges.json"
# The watermark keys that carry the merge scan: the default branch and the sha
# the last scan reached. A sync that does not scan leaves them untouched.
SCAN_KEYS = ("merge_scan_base", "merge_scan_sha")
# GitHub names a squash commit "<PR title> (#<number>)".
_SQUASH_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$")
_TICKET_RE = re.compile(r"\b(OMN-\d+)\b", re.IGNORECASE)
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
# True when an event for (repo, pr_number) was already published by another
# producer (the Actions publisher, the webhook fold). No GitHub call.
PublishedMerges = Callable[[str, int], bool]


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
        published_merges: PublishedMerges | None = None,
    ) -> None:
        self.root = root
        self.published_merges = published_merges
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

    def sync(self, repo: str, detect_merges: bool = True) -> ModelGitQueryResult:
        """Create the mirror if needed, then fetch every head and PR ref (fetch only).

        With ``detect_merges`` the result's ``merged_prs`` lists the squash merges
        that landed on the default branch since the previous detecting sync; the
        events are durably pending before the scan watermark moves. Only an
        acknowledged publish may move an event to the published set.
        """
        mirror = self.mirror_path(repo)
        merged: list[ModelMirrorPrMerged] | None = None
        try:
            with self._lock(mirror):
                if not (mirror / "HEAD").exists():
                    err = self._create(repo, mirror)
                    if err:
                        return self._result_err(
                            EnumGitQueryOperation.SYNC, repo, None, mirror, err
                        )
                heads_before = self._head_shas(mirror)
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
                    self._write_json(mirror / WATERMARK_FILE, prior)
                    return self._result_err(
                        EnumGitQueryOperation.SYNC,
                        repo,
                        None,
                        mirror,
                        f"fetch failed: {_tail(err)}",
                    )
                pulls = self._count(mirror, "refs/pull")
                heads = self._count(mirror, "refs/heads")
                scan = {k: prior[k] for k in SCAN_KEYS if k in prior}
                if detect_merges:
                    merged, scan = self._scan_merges(
                        repo, mirror, prior, heads_before, now
                    )
                    merged = self._queue_merges(mirror, merged or [])
                wm = {
                    **scan,
                    "repo": repo,
                    "ok": True,
                    "fetched_at": _iso(now),
                    "fetched_at_epoch": now,
                    "pull_refs": pulls,
                    "head_refs": heads,
                    "last_attempt_at": _iso(now),
                }
                self._write_json(mirror / WATERMARK_FILE, wm)
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
            merged_prs=merged,
        )

    # -- merge detection -------------------------------------------------------

    def _head_shas(self, mirror: Path) -> dict[str, str]:
        code, out, _ = _git(
            mirror,
            "for-each-ref",
            "--format=%(objectname) %(refname:short)",
            "refs/heads",
        )
        if code != 0:
            return {}
        shas: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, name = line.partition(" ")
            if name:
                shas[name] = sha
        return shas

    @staticmethod
    def _write_json(path: Path, data: dict[str, object]) -> None:
        """Persist before advancing the scan or acknowledging delivery.

        Like the emit node's spool, replace a complete file atomically. Sync
        both the file and directory so a host restart preserves the ordering.
        """
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def _published_merge_numbers(self, mirror: Path) -> set[int]:
        """Read the legacy published set; fail closed on corruption."""
        try:
            data = json.loads((mirror / PUBLISHED_MERGES_FILE).read_text())
        except FileNotFoundError:
            return set()
        if not isinstance(data, dict) or not isinstance(data.get("prs"), list):
            raise ValueError("invalid mirror merge outbox")
        prs = data["prs"]
        if any(type(n) is not int for n in prs):
            raise ValueError("invalid mirror merge outbox")
        return set(prs)

    def _merge_spool(self, mirror: Path) -> SpoolOutbox:
        return SpoolOutbox(mirror / "onex_merge_outbox")

    def _pending_files(self, mirror: Path) -> list[SpoolFile]:
        spool = self._merge_spool(mirror)
        files = spool.list_pending()
        if len(files) != spool.pending_count():
            raise ValueError("unreadable mirror merge outbox record")
        return files

    def _queue_merges(
        self, mirror: Path, events: list[ModelMirrorPrMerged]
    ) -> list[ModelMirrorPrMerged]:
        """Caller holds the mirror lock; save the payload before moving the watermark."""
        published = self._published_merge_numbers(mirror)
        pending = self._pending_merges(mirror)
        seen = published | {e.pr_number for e in pending}
        spool = self._merge_spool(mirror)
        for event in events:
            if event.pr_number not in seen:
                spool.append(
                    SpoolRecord(
                        event_id=str(event.event_id),
                        event_type="MirrorPrMerged",
                        topic=event.topic,
                        tier=EnumDurabilityTier.DUTY_CRITICAL,
                        payload=event.model_dump(mode="json"),
                        partition_key=event.repo,
                        correlation_id=None,
                        queued_at=datetime.fromtimestamp(self.clock(), tz=UTC),
                    )
                )
                pending.append(event)
                seen.add(event.pr_number)
        return pending

    def pending_merges(self, repo: str) -> list[ModelMirrorPrMerged]:
        """Payloads owed to the bus, including those left by a previous process."""
        mirror = self.mirror_path(repo)
        if not (mirror / "HEAD").exists():
            return []
        return self._pending_merges(mirror)

    def _pending_merges(self, mirror: Path) -> list[ModelMirrorPrMerged]:
        published = self._published_merge_numbers(mirror)
        return [
            event
            for file in self._pending_files(mirror)
            if (
                event := ModelMirrorPrMerged.model_validate(file.record.payload)
            ).pr_number
            not in published
        ]

    def mark_merge_published(self, event: ModelMirrorPrMerged) -> None:
        """Acknowledge one exact pending event, only after the publisher returns success."""
        mirror = self.mirror_path(event.repo)
        with self._lock(mirror):
            published = self._published_merge_numbers(mirror)
            matching = [
                file
                for file in self._pending_files(mirror)
                if file.record.event_id == str(event.event_id)
            ]
            if event.pr_number not in published and not matching:
                raise ValueError(
                    "cannot acknowledge an event absent from the mirror outbox"
                )
            # Persist the published marker before deleting the spool record.
            # A crash between them leaves an already-published file, never an
            # unrecorded loss. The pending reader filters that file on restart.
            self._write_json(
                mirror / PUBLISHED_MERGES_FILE,
                {"prs": sorted(published | {event.pr_number})},
            )
            for file in matching:
                self._merge_spool(mirror).ack(file.path)

    def is_merge_published(self, repo: str, pr_number: int) -> bool:
        """True when the injected lookup or this mirror's own ledger holds the PR."""
        if self.published_merges is not None and self.published_merges(repo, pr_number):
            return True
        return pr_number in self._published_merge_numbers(self.mirror_path(repo))

    def _scan_merges(
        self,
        repo: str,
        mirror: Path,
        prior: dict[str, object],
        heads_before: dict[str, str],
        now: float,
    ) -> tuple[list[ModelMirrorPrMerged] | None, dict[str, object]]:
        """Squash merges on the default branch since the last scan, and the new scan keys.

        The first scan only records where the branch stands: it publishes no
        history. A base branch that was rewritten (the last scanned sha is no
        longer an ancestor) is re-baselined the same way. A failed git read
        keeps the previous scan keys so the next sync retries the same range.
        """
        carried = {k: prior[k] for k in SCAN_KEYS if k in prior}
        base = self._default_branch(mirror)
        code, new_sha, _ = _git(
            mirror, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}"
        )
        if code != 0:
            return None, carried
        new_sha = new_sha.strip()
        fresh: dict[str, object] = {"merge_scan_base": base, "merge_scan_sha": new_sha}
        prev = prior.get("merge_scan_sha")
        if prior.get("merge_scan_base") != base or not isinstance(prev, str):
            return [], fresh
        if prev == new_sha:
            return [], fresh
        if _git(mirror, "merge-base", "--is-ancestor", prev, new_sha)[0] != 0:
            return [], fresh
        code, out, _ = _git(
            mirror,
            "log",
            "--first-parent",
            "--reverse",
            "--format=%H%x1f%s%x1f%cI",
            f"{prev}..{new_sha}",
        )
        if code != 0:
            return None, carried
        heads_after = self._head_shas(mirror)
        published = self._published_merge_numbers(mirror)
        events: list[ModelMirrorPrMerged] = []
        for line in out.splitlines():
            sha, subject, merged_at = [*line.split("\x1f"), "", ""][:3]
            found = _SQUASH_SUBJECT_RE.search(subject)
            if found is None or not merged_at:
                continue
            number = int(found.group(1))
            if number in published or self.is_merge_published(repo, number):
                continue
            events.append(
                self._merge_event(
                    repo,
                    mirror,
                    base,
                    number,
                    sha,
                    subject,
                    merged_at,
                    {**heads_before, **heads_after},
                    now,
                )
            )
            published.add(number)
        return events, fresh

    def _merge_event(
        self,
        repo: str,
        mirror: Path,
        base: str,
        number: int,
        merge_sha: str,
        subject: str,
        merged_at: str,
        head_shas: dict[str, str],
        now: float,
    ) -> ModelMirrorPrMerged:
        code, head, _ = _git(
            mirror, "rev-parse", "--verify", "--quiet", f"refs/pull/{number}/head"
        )
        head_sha = head.strip() if code == 0 and head.strip() else None
        names = sorted(
            n for n, sha in head_shas.items() if sha == head_sha and n != base
        )
        branch = names[0] if head_sha and names else f"refs/pull/{number}/head"
        ticket = ""
        for text in (branch, subject):
            ticket_found = _TICKET_RE.search(text)
            if ticket_found:
                ticket = ticket_found.group(1).upper()
                break
        return ModelMirrorPrMerged(
            event_id=pr_merged_event_id(repo, number, merge_sha),
            repo=repo,
            branch=branch,
            branch_resolved=bool(head_sha and names),
            base_ref=base,
            pr_number=number,
            ticket=ticket,
            merge_sha=merge_sha,
            head_sha=head_sha,
            merged_at=merged_at,
            published_at=_iso(now),
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
            return self.sync(request.repo, detect_merges=request.detect_merges)
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
            built = self.sync(request.repo, detect_merges=False)
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
