# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify that an install contains the live commits or declares each missing commit as an intentional drop."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.install_file_list import (
    COPIED,
    SKIPPED,
    SupersetError,
    file_list_comparison,
    git,
    install_parts,
    ls_tree,
    resolve,
    text,
)

from ..models.model_install_superset import (
    ModelCarriedCommit,
    ModelMissingCommit,
    ModelSupersetRequest,
    ModelSupersetResult,
)

LANE_RE = re.compile("\\blane[= ]([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])")


def try_resolve(repo: Path, ref: str) -> str:
    try:
        return resolve(repo, ref)
    except SupersetError:
        return ""


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return (
        git(repo, "merge-base", "--is-ancestor", older, newer, ok=(0, 1)).returncode
        == 0
    )


def rev_list(repo: Path, spec: str) -> list[str]:
    return text(git(repo, "rev-list", "--no-merges", "--reverse", spec)).split()


def subjects(repo: Path, shas: list[str]) -> dict[str, str]:
    if not shas:
        return {}
    out = text(
        git(
            repo,
            "log",
            "--stdin",
            "--no-walk=unsorted",
            "--format=%H %s",
            data="\n".join(shas).encode(),
        )
    )
    named: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, subject = line.partition(" ")
        if sha:
            named[sha] = subject
    return named


def patch_ids(repo: Path, shas: list[str]) -> dict[str, str]:
    """``{commit: stable patch-id}``; a commit with an empty diff has none."""
    if not shas:
        return {}
    log = git(
        repo,
        "log",
        "--stdin",
        "--no-walk=unsorted",
        "-p",
        "--no-color",
        "--format=commit %H",
        data="\n".join(shas).encode(),
    ).stdout
    out = text(git(repo, "patch-id", "--stable", data=log))
    return {
        sha: pid
        for pid, sha in (line.split() for line in out.splitlines() if line.strip())
    }


def diff_present(repo: Path, commit: str, tree: str) -> bool:
    """True when ``commit``'s own diff reverse-applies to ``tree``: its change is already there."""
    parents = text(git(repo, "rev-list", "--parents", "-n", "1", commit)).split()[1:]
    if len(parents) != 1:
        return False
    patch = git(
        repo, "diff", "--binary", "--full-index", "--no-color", parents[0], commit
    ).stdout
    if not patch.strip():
        return True
    with tempfile.TemporaryDirectory(prefix="install-superset-") as scratch:
        env = {
            **scrub_git_location_env(os.environ),
            "GIT_INDEX_FILE": str(Path(scratch) / "index"),
        }
        git(repo, "read-tree", tree, env=env)
        done = git(
            repo,
            "apply",
            "--cached",
            "--check",
            "-R",
            "--whitespace=nowarn",
            data=patch,
            env=env,
            ok=(0, 1, 128),
        )
        return done.returncode == 0


def history_lane(history: str, commit: str) -> str:
    """The lane of the first INSTALLED-FROM-BRANCHES.txt entry naming ``commit`` (8 characters)."""
    if not history:
        return ""
    for paragraph in re.split("\\n\\s*\\n", history):
        if commit[:8] in paragraph:
            found = LANE_RE.search(paragraph)
            if found:
                return found.group(1)
    return ""


class HandlerInstallSuperset:
    """``handle(request) -> result``: is every commit of the live install held by the new one?"""

    def handle(self, request: ModelSupersetRequest) -> ModelSupersetResult:
        repo = request.repo
        new = resolve(repo, request.new_commit)
        live = resolve(repo, request.live_commit) if request.live_commit else ""
        held: list[tuple[str, str]] = []
        lacking: list[str] = []
        if live and is_ancestor(repo, live, new):
            held.append((live, "ancestor"))
        elif live:
            short = rev_list(repo, f"{new}..{live}")
            pool = set(patch_ids(repo, rev_list(repo, f"{live}..{new}")).values())
            ids = patch_ids(repo, short)
            for commit in short:
                if commit not in ids:
                    held.append((commit, "empty"))
                elif ids[commit] in pool:
                    held.append((commit, "patch-id"))
                elif diff_present(repo, commit, new):
                    held.append((commit, "diff"))
                else:
                    lacking.append(commit)
        named = subjects(repo, lacking)
        main = try_resolve(repo, request.main_ref)
        by_sha = {c.commit: c.lane for c in request.live_carried}
        by_pid = {c.patch_id: c.lane for c in request.live_carried if c.patch_id}
        lacking_ids = patch_ids(repo, lacking)

        def lane_of(commit: str) -> str:
            return (
                by_sha.get(commit)
                or by_pid.get(lacking_ids.get(commit, ""), "")
                or history_lane(request.history, commit)
                or (
                    "main" if main and is_ancestor(repo, commit, main) else "unrecorded"
                )
            )

        drops = {resolve(repo, d.commit): d for d in request.drops}
        missing = tuple(
            ModelMissingCommit(commit=c, lane=lane_of(c), subject=named.get(c, ""))
            for c in lacking
            if c not in drops
        )
        dropped = tuple(
            ModelMissingCommit(commit=c, lane=lane_of(c), subject=named.get(c, ""))
            for c in lacking
            if c in drops
        )
        unused = tuple(sha for sha in drops if sha not in lacking)
        verdict = (
            "refused" if missing or unused else "dropped" if dropped else "contained"
        )
        return ModelSupersetResult(
            verdict=verdict,
            new_commit=new,
            live_commit=live,
            missing=missing,
            dropped=dropped,
            held=tuple(held),
            carried=self.carried(request, new, live, main),
            unused_drops=unused,
        )

    @staticmethod
    def carried(
        request: ModelSupersetRequest, new: str, live: str, main: str
    ) -> tuple[ModelCarriedCommit, ...]:
        """What the new install carries beyond main, each with the lane that first carried it.

        A commit no record names is this install's lane only when the live install lacked it;
        one the live install already held is "unrecorded" rather than credited to this lane.
        """
        repo = request.repo
        shas = rev_list(
            repo, f"{main}..{new}" if main else f"{live}..{new}" if live else new
        )
        ids, named = (patch_ids(repo, shas), subjects(repo, shas))
        by_sha = {c.commit: c.lane for c in request.live_carried}
        by_pid = {c.patch_id: c.lane for c in request.live_carried if c.patch_id}
        old = (
            set(rev_list(repo, f"{main}..{live}" if main else live)) if live else set()
        )
        return tuple(
            ModelCarriedCommit(
                commit=c,
                patch_id=ids.get(c, ""),
                subject=named.get(c, ""),
                lane=by_sha.get(c)
                or by_pid.get(ids.get(c, ""), "")
                or history_lane(request.history, c)
                or ("unrecorded" if c in old else request.lane),
            )
            for c in shas
        )

    @staticmethod
    def lost_message(commit: str, origin: str, lost: list[str]) -> str:
        """The refusal text for a live tree that lacks files the installed commit's copy holds."""
        return (
            f"LIVE-TREE-MISSING: the live tree lacks {len(lost)} file(s) {commit[:12]} ({origin}) installs: "
            f"{', '.join(lost[:8])}. Reinstall it before building on it"
        )


def blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(
        b"blob %d\x00" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def tree_differences(
    repo: Path, commit: str, prefix: str, install_dir: Path
) -> tuple[int, list[str]]:
    """``(files compared, files that differ)`` between the installed controller tree and ``commit``.

    The installer copies ``<prefix>`` to ``skills/``, ``<prefix>/../scripts`` to ``scripts/`` and
    ``<prefix>/../src`` to ``src/``. A file the commit does not have is not compared, nor one no
    install copies.
    """
    compared, differ = (0, [])
    for part, in_repo in install_parts(repo, commit, prefix).items():
        root = install_dir / part
        if not root.is_dir():
            continue
        listing = ls_tree(repo, commit, in_repo)
        paths = sorted({p for pattern in COPIED[part] for p in root.glob(pattern)})
        for path in paths:
            if not path.is_file() or any(s in path.parts for s in SKIPPED):
                continue
            entry = listing.get(path.relative_to(root).as_posix())
            if entry is None or entry[0] == "120000":
                continue
            compared += 1
            if blob_sha(path) != entry[1]:
                differ.append(f"{part}/{path.relative_to(root).as_posix()}")
    return (compared, differ)


def tree_state(
    repo: Path, commit: str, prefix: str, install_dir: Path, superset: dict[str, Any]
) -> tuple[int, list[str], list[str]]:
    """``(files compared, files that differ, files the install lacks)`` for the live tree.

    The prefix is the one the live install's own record names (``superset["prefix"]``), else ``prefix``.
    """
    live_prefix = str(superset.get("prefix") or prefix)
    compared, differ = tree_differences(repo, commit, live_prefix, install_dir)
    return (
        compared,
        differ,
        file_list_comparison(repo, commit, live_prefix, install_dir)[1],
    )
