# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host discovery and conservative git/process/ledger fact collection."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from omnimarket.events.worktree_reconcile import EnumWorktreeKind as Kind
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeFacts,
    ModelWorktreeReconcileCommand,
)
from omnimarket.handlers.handler_ledger_claims import claimed, live_claims
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_commands import (
    git,
)

Operation = Literal["rebase", "merge", "cherry-pick", "bisect"]

# Trees probed at once. Each probe is a handful of short git and du processes.
PROBE_WORKERS = 6

_JUNK = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".tox",
    "htmlcov",
    "dist",
    "build",
}


def is_secret(name: str) -> bool:
    return any(
        part.startswith(".env") or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in Path(name).parts
    )


def is_junk(name: str) -> bool:
    parts = Path(name).parts
    # Secrets stay protected even when nested inside a regenerable directory.
    if is_secret(name):
        return False
    return any(part in _JUNK or part.endswith(".egg-info") for part in parts)


def process_cwds() -> tuple[Path, ...]:
    """Every visible process's working directory, once per call.

    Linux reads ``/proc/<pid>/cwd`` (another user's process is unreadable and is
    not this user's lane). Elsewhere ``lsof`` is used; its stderr carries routine
    warnings (an unstatable mount), so only a failed or empty run is an error.
    """
    proc_root = Path("/proc")
    if sys.platform.startswith("linux") and proc_root.is_dir():
        cwds: list[Path] = []
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cwds.append(Path(os.readlink(entry / "cwd")).resolve())
            except OSError:
                continue
        if not cwds:
            raise RuntimeError("process snapshot empty")
        return tuple(cwds)
    proc = subprocess.run(
        ["lsof", "-n", "-P", "-d", "cwd", "-Fpn"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    found = tuple(
        Path(line[1:]).resolve()
        for line in proc.stdout.splitlines()
        if line.startswith("n/")
    )
    if proc.returncode not in (0, 1) or not found:
        raise RuntimeError("process snapshot incomplete")
    return found


def _fetch_all(clone: Path) -> bool:
    """Refresh every remote of one clone. False on any failure (the caller keeps)."""
    try:
        for remote in git(str(clone), "remote").stdout.splitlines():
            git(str(clone), "fetch", "--quiet", "--prune", remote, timeout=300)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False
    return True


def _linked_activity(
    path: Path, newest: float, *, measure_size: bool = True
) -> tuple[list[str], float, int]:
    """Changed and untracked names, latest activity and on-disk size, from git.

    Activity is the newest of the HEAD commit, the tree's own HEAD reflog (a
    checkout, a reset, a commit by a lane) and the mtimes of its changed and
    untracked files. Size is ``du``, junk included, since that is what removal
    frees; an unmeasurable size is left unset rather than guessed. A
    revalidation skips ``du``: size decides nothing, and on a loaded host it is
    the slowest part of a probe.
    """
    listing = git(
        str(path),
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout.split("\0")
    names = sorted(
        {entry[3:] for entry in listing if len(entry) > 3 and not is_junk(entry[3:])}
    )
    reflog = git(
        str(path), "reflog", "-1", "--format=%ct", "HEAD", ok=(0, 128)
    ).stdout.strip()
    if reflog.isdigit():
        newest = max(newest, float(reflog))
    for name in names[:500]:
        try:
            newest = max(newest, (path / name).lstat().st_mtime)
        except OSError:
            continue
    size = 0
    if not measure_size:
        return names, newest, size
    try:
        du = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        size = int(du.stdout.split()[0]) * 1024 if du.returncode == 0 else 0
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        size = 0
    return names, newest, size


def repo_slug(url: str) -> str | None:
    path = urlsplit(url).path if "://" in url else url.partition(":")[2]
    parts = path.strip("/").removesuffix(".git").split("/")
    return (
        "/".join(parts)
        if len(parts) == 2 and all(re.fullmatch(r"[\w.-]+", part) for part in parts)
        else None
    )


class GitWorktreeFactsProbe:
    def __init__(self) -> None:
        self._command: ModelWorktreeReconcileCommand | None = None
        self._cwds: tuple[Path, ...] = ()
        self._claims: tuple[str, ...] = ()
        self._errors: tuple[str, ...] = ()
        # Remote refs are refreshed once per canonical clone per run, not once per
        # worktree: keyed by the clone's common git dir, True when the fetch worked.
        self._fetched: dict[Path, bool] = {}
        self._fetch_lock = threading.Lock()

    def discover(
        self, command: ModelWorktreeReconcileCommand, now: datetime
    ) -> tuple[ModelWorktreeFacts, ...]:
        self._command = command
        errors = []
        try:
            self._cwds = process_cwds()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            self._cwds = ()
            errors.append("process_snapshot_failed")
        # A shared ledger is appended to every few seconds, so a changed ledger is
        # the normal state, not an error: claims are re-read before each removal.
        try:
            self._claims = self._read_claims(now)
        except OSError:
            self._claims = ()
            errors.append("ledger_unreadable")
        excluded = tuple(Path(p).resolve() for p in command.exclude_paths)
        self._fetched = {}
        candidates: dict[Path, Path] = {}
        roots = tuple(Path(root).resolve() for root in command.roots)
        registries = tuple(root.parent for root in roots)
        canonical: set[Path] = set()
        standalone: set[Path] = set()
        for root in roots:
            try:
                for clone in sorted(root.parent.iterdir()):
                    if clone.is_symlink() or not (clone / ".git").is_dir():
                        continue
                    canonical.add(clone.resolve())
                    self._fetched[(clone / ".git").resolve()] = _fetch_all(clone)
                    listing = git(
                        str(clone), "worktree", "list", "--porcelain", "-z"
                    ).stdout
                    for line in listing.split("\0"):
                        if line.startswith("worktree "):
                            path = Path(line[9:]).resolve()
                            if path != root and path.is_relative_to(root):
                                candidates[path] = root
                for ticket in sorted(root.iterdir()):
                    if (
                        ticket.is_symlink()
                        or not ticket.is_dir()
                        or ticket.name.startswith(".")
                    ):
                        continue
                    if (ticket / ".git").exists() or not any(ticket.iterdir()):
                        candidates[ticket] = root
                    else:
                        children = sorted(ticket.iterdir())
                        directories = [
                            path
                            for path in children
                            if path.is_dir() and not path.is_symlink()
                        ]
                        if not directories:
                            candidates[ticket] = root
                        for path in directories:
                            candidates[path.resolve()] = root
            except (OSError, subprocess.SubprocessError, RuntimeError):
                errors.append("discovery_failed")
        for extra in command.extra_clone_roots:
            root = Path(extra).resolve()
            # Registry roots are never a standalone-clone source. Explicit
            # extra roots inside a registry are also refused.
            if any(
                root == registry or root.is_relative_to(registry)
                for registry in registries
            ):
                continue
            try:
                level = [root]
                for _ in range(3):
                    next_level: list[Path] = []
                    for path in level:
                        if path.is_symlink() or path.name == ".onex_state":
                            continue
                        real = path.resolve()
                        if any(
                            real == ex or real.is_relative_to(ex) for ex in excluded
                        ):
                            continue
                        if any(
                            real == registry or real.is_relative_to(registry)
                            for registry in registries
                        ):
                            continue
                        try:
                            is_clone = (path / ".git").is_dir()
                            children = [] if is_clone else sorted(path.iterdir())
                        except PermissionError:
                            # Not this user's directory, so not a lane's clone.
                            continue
                        if is_clone:
                            if path != root:
                                candidates[real] = root
                                standalone.add(real)
                            continue
                        next_level.extend(
                            p for p in children if p.is_dir() and not p.is_symlink()
                        )
                    level = next_level
            except OSError:
                errors.append("clone_discovery_failed")
        self._errors = tuple(errors)
        selected = [
            (path, root)
            for path, root in sorted(candidates.items())
            if path not in canonical
            and (not (path / ".git").is_dir() or path in standalone)
        ]
        # An error with nothing to attach it to would vanish into a run that
        # reports scanned=0 and reads as a clean host. Raise so the run says so.
        if errors and not selected:
            raise RuntimeError(f"discovery incomplete: {', '.join(errors)}")
        # Probing is subprocess-bound (git, du), one tree independent of the next,
        # so trees are probed concurrently; results keep the sorted order.
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            return tuple(
                pool.map(lambda item: self._probe(item[0], item[1], now), selected)
            )

    def _read_claims(self, now: datetime) -> tuple[str, ...]:
        assert self._command is not None
        if not self._command.ledger_path:
            return ()
        text = Path(self._command.ledger_path).read_text(encoding="utf-8")
        return live_claims(text, now, self._command.policy.quiet_hours)

    def revalidate(
        self, facts: ModelWorktreeFacts, now: datetime
    ) -> ModelWorktreeFacts:
        # Initial discovery uses one snapshot for the entire run. Removal gets
        # a fresh snapshot so processes started since discovery protect a tree.
        # Revalidations of different repositories run concurrently, so the fresh
        # process and claim snapshots are local to this call.
        fresh = self._probe(Path(facts.path), Path(facts.root), now, measure_size=False)
        errors = list(self._errors)
        cwds = self._cwds
        claims = self._claims
        try:
            cwds = process_cwds()
        except (OSError, subprocess.SubprocessError, RuntimeError):
            errors.append("process_revalidation_failed")
        try:
            claims = self._read_claims(now)
        except OSError:
            errors.append("ledger_unreadable")
        path = Path(facts.path)
        return fresh.model_copy(
            update={
                "probe_errors": tuple(errors) + fresh.probe_errors,
                "live_process": any(
                    cwd == path or cwd.is_relative_to(path) for cwd in cwds
                ),
                "live_claim": claimed(path, Path(facts.root), claims),
                # Size is not re-measured; keep what discovery measured rather
                # than a 0 that could read as an empty tree.
                "size_bytes": facts.size_bytes,
            }
        )

    def _probe(
        self, path: Path, root: Path, now: datetime, *, measure_size: bool = True
    ) -> ModelWorktreeFacts:
        assert self._command is not None
        kind = (
            Kind.STANDALONE_CLONE
            if (path / ".git").is_dir()
            else Kind.LINKED_WORKTREE
            if (path / ".git").is_file()
            else Kind.ORPHAN_DIR
        )
        facts = ModelWorktreeFacts(
            host=self._command.host,
            path=str(path),
            root=str(root),
            kind=kind,
            live_process=any(
                cwd == path or cwd.is_relative_to(path) for cwd in self._cwds
            ),
            live_claim=claimed(path, root, self._claims),
            probe_errors=self._errors,
        )
        try:
            if (
                path.is_symlink()
                or path == root
                or not path.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("unsafe_path")
            names: list[str] = []
            size = 0
            newest = 0.0
            walk = (
                ()
                if kind == Kind.LINKED_WORKTREE
                else os.walk(path, followlinks=False, onerror=_raise_walk_error)
            )
            for base, dirs, files in walk:
                if (
                    Path(base) != path
                    and ".git" not in Path(base).relative_to(path).parts
                    and (".git" in dirs or ".git" in files)
                ):
                    raise ValueError("nested_repository_requires_human_review")
                # Regenerable trees are neither activity nor work, and walking a
                # .venv or node_modules per tree costs more than the whole scan.
                dirs[:] = [
                    d
                    for d in dirs
                    if not is_junk(d) and (kind == Kind.STANDALONE_CLONE or d != ".git")
                ]
                for name in files:
                    file = Path(base) / name
                    relative = str(file.relative_to(path))
                    if relative == ".git":
                        continue
                    stat = file.lstat()
                    size += stat.st_size
                    if ".git" not in Path(relative).parts and not is_junk(relative):
                        names.append(relative)
                        newest = max(newest, stat.st_mtime)
            if kind == Kind.ORPHAN_DIR:
                empty = not any(path.iterdir())
                return facts.model_copy(
                    update={
                        "orphan_empty": empty,
                        "size_bytes": size,
                        "file_names": tuple(sorted(names)),
                        "last_activity_age_hours": (
                            now.timestamp() - (newest or path.stat().st_mtime)
                        )
                        / 3600,
                    }
                )
            remotes = git(str(path), "remote").stdout.splitlines()
            common_dir = Path(
                git(
                    str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"
                ).stdout.strip()
            ).resolve()
            with self._fetch_lock:
                if common_dir not in self._fetched:
                    self._fetched[common_dir] = _fetch_all(common_dir)
            # Stale remote-tracking refs could vouch for a branch the remote has
            # already deleted, so no remote evidence is trusted without a refresh.
            if remotes and not self._fetched[common_dir]:
                raise ValueError("remote_refresh_failed")
            head = git(str(path), "rev-parse", "HEAD").stdout.strip()
            branch_result = git(
                str(path), "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1)
            )
            branch = branch_result.stdout.strip() or None
            refs = git(
                str(path), "for-each-ref", "--format=%(objectname)", "refs/remotes"
            ).stdout.splitlines()

            def on_remote(sha: str) -> bool:
                # One rev-list, not one merge-base per remote ref: a registry clone
                # carries thousands of remote branches.
                count = git(
                    str(path), "rev-list", "--count", sha, "--not", "--remotes"
                ).stdout.strip()
                return bool(refs) and count == "0"

            unpushed_branches = git(
                str(path), "rev-list", "--count", "--branches", "--not", "--remotes"
            ).stdout.strip()
            all_remote = bool(refs) and unpushed_branches == "0"
            # Deleting a clone must not break linked worktrees elsewhere.
            if (
                kind == Kind.STANDALONE_CLONE
                and git(str(path), "worktree", "list", "--porcelain").stdout.count(
                    "worktree "
                )
                > 1
            ):
                raise ValueError("clone_has_linked_worktrees")
            status = git(
                str(path),
                "--no-optional-locks",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=no",
                "--ignore-submodules=none",
            ).stdout.split("\0")
            dirty = sum(bool(line) and not line.startswith("??") for line in status)
            # Untracked work is what the repository does not ignore. An ignored
            # file is a build or tool cache, except a secrets-shaped one (a .env,
            # a key), which is never regenerable and always counts.
            untracked = git(
                str(path), "ls-files", "--others", "--exclude-standard", "-z"
            ).stdout.split("\0")
            ignored = git(
                str(path),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                ".",
                *(f":(exclude,glob)**/{junk}/**" for junk in sorted(_JUNK)),
            ).stdout.split("\0")
            nonjunk = sum(bool(name) and not is_junk(name) for name in untracked) + sum(
                bool(name) and is_secret(name) for name in ignored
            )
            # refs/stash and its reflog live in the clone's common git dir: every
            # linked worktree of a clone lists the same stashes, and removing one
            # worktree leaves them all in place. Only removing a standalone clone
            # loses its stashes, so only a clone counts them. Counting them on a
            # linked worktree held every clean worktree of a clone with any stash
            # at needs-human.
            stashes: list[str] = []
            if kind == Kind.STANDALONE_CLONE:
                stash_ref = git(
                    str(path),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/stash",
                    ok=(0, 1),
                )
                if stash_ref.returncode == 0:
                    stashes = git(
                        str(path), "reflog", "show", "--format=%H", "refs/stash"
                    ).stdout.splitlines()
            stash_count = sum(not on_remote(sha) for sha in stashes)
            gitdir = Path(
                git(str(path), "rev-parse", "--absolute-git-dir").stdout.strip()
            )
            operation: Operation | None = None
            operations: tuple[tuple[str, Operation], ...] = (
                ("rebase-merge", "rebase"),
                ("rebase-apply", "rebase"),
                ("MERGE_HEAD", "merge"),
                ("CHERRY_PICK_HEAD", "cherry-pick"),
                ("BISECT_LOG", "bisect"),
            )
            for marker, op in operations:
                if (gitdir / marker).exists():
                    operation = op
                    break
            # This tree's own locks only. A lock elsewhere in the shared common dir
            # belongs to a sibling worktree and says nothing about this one.
            locked = any(
                (gitdir / marker).exists()
                for marker in ("index.lock", "HEAD.lock", "locked")
            )
            merged = False
            # Merged means merged into this repository's own default branch. A
            # clone may also carry remotes of other repositories (unrelated
            # histories, where merge-tree refuses), so origin alone is asked when
            # it exists, and a refusal there means "not merged", never an error.
            bases = ["origin"] if "origin" in remotes else remotes
            for remote in bases:
                base = git(
                    str(path),
                    "symbolic-ref",
                    "--quiet",
                    f"refs/remotes/{remote}/HEAD",
                    ok=(0, 1),
                ).stdout.strip()
                if not base:
                    continue
                if (
                    git(
                        str(path),
                        "merge-base",
                        "--is-ancestor",
                        "HEAD",
                        base,
                        ok=(0, 1),
                    ).returncode
                    == 0
                ):
                    merged = True
                    break
                tree = git(
                    str(path),
                    "merge-tree",
                    "--write-tree",
                    base,
                    "HEAD",
                    ok=(0, 1, 128),
                ).stdout.splitlines()
                base_tree = git(
                    str(path), "rev-parse", f"{base}^{{tree}}"
                ).stdout.strip()
                if tree and tree[0] == base_tree:
                    merged = True
                    break
            newest = max(
                newest,
                float(git(str(path), "show", "-s", "--format=%ct", "HEAD").stdout),
            )
            if kind == Kind.LINKED_WORKTREE:
                names, newest, size = _linked_activity(
                    path, newest, measure_size=measure_size
                )
            slug = (
                repo_slug(
                    git(str(path), "remote", "get-url", remotes[0]).stdout.strip()
                )
                if remotes
                else None
            )
            return facts.model_copy(
                update={
                    "repo_slug": slug,
                    "remote_owner": slug.split("/")[0] if slug else None,
                    "branch": branch,
                    "head_sha": head,
                    "detached": branch is None,
                    "dirty_tracked_count": dirty,
                    "untracked_nonjunk_count": nonjunk,
                    "unpushed_stash_count": stash_count,
                    "in_progress_operation": operation,
                    "git_locked": locked,
                    "head_on_remote": on_remote(head),
                    "content_merged": merged,
                    "commits_not_on_remote": int(
                        git(
                            str(path),
                            "rev-list",
                            "--count",
                            "HEAD",
                            "--not",
                            "--remotes",
                        ).stdout
                    ),
                    "local_branches_all_on_remote": all_remote,
                    "has_remote": bool(remotes),
                    "last_activity_age_hours": (now.timestamp() - newest) / 3600,
                    "size_bytes": size,
                    "file_names": tuple(sorted(names)),
                    "diff_stat": git(str(path), "diff", "HEAD", "--stat").stdout,
                    "commit_subjects": tuple(
                        git(str(path), "log", "-20", "--format=%s").stdout.splitlines()
                    ),
                }
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            # Do not leak subprocess arguments or remote credentials.
            return facts.model_copy(
                update={
                    "probe_errors": (
                        *facts.probe_errors,
                        f"probe_failed:{type(exc).__name__}",
                    )
                }
            )


def _raise_walk_error(error: OSError) -> None:
    raise error
