# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify that an install contains the live commits or declares each missing commit as an intentional drop."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path

from ..models.model_install_superset import (
    ModelCarriedCommit,
    ModelMissingCommit,
    ModelSupersetRequest,
    ModelSupersetResult,
)

GIT_TIMEOUT_S = 120
LANE_RE = re.compile("\\blane[= ]([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])")


class SupersetError(RuntimeError):
    """A git read the guard needs failed; the guard refuses rather than guess."""


def git(
    repo: Path,
    *args: str,
    data: bytes | None = None,
    env: dict[str, str] | None = None,
    ok: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=data,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SupersetError(f"git {' '.join(args[:2])}: {exc}") from exc
    if done.returncode not in ok:
        raise SupersetError(
            f"git {' '.join(args[:3])}: {done.stderr.decode(errors='replace').strip()[:300]}"
        )
    return done


def text(done: subprocess.CompletedProcess[bytes]) -> str:
    return done.stdout.decode(errors="replace")


def resolve(repo: Path, ref: str) -> str:
    done = git(repo, "rev-parse", "--verify", "-q", ref + "^{commit}", ok=(0, 1, 128))
    sha = text(done).strip()
    if done.returncode != 0 or not sha:
        raise SupersetError(
            f"{ref} is no commit in {repo}; fetch it into that repository first"
        )
    return sha


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
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
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


def blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(
        b"blob %d\x00" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def ls_tree(repo: Path, commit: str, path: str) -> dict[str, tuple[str, str]]:
    """``{path below `path`: (mode, blob)}`` at ``commit``."""
    out = git(
        repo, "ls-tree", "-r", "-z", commit, "--", path.rstrip("/") + "/"
    ).stdout.decode(errors="replace")
    listing: dict[str, tuple[str, str]] = {}
    for item in out.split("\x00"):
        if not item:
            continue
        meta, _, name = item.partition("\t")
        mode, _, rest = meta.partition(" ")
        listing[name[len(path.rstrip("/")) + 1 :]] = (mode, rest.split()[-1])
    return listing


def linked_dir(repo: Path, commit: str, path: str) -> str:
    """``path`` itself, or the repository directory it links to when it is a symlink."""
    out = (
        git(repo, "ls-tree", "-z", commit, "--", path)
        .stdout.decode(errors="replace")
        .strip("\x00")
    )
    if not out.startswith("120000 "):
        return path
    target = git(repo, "cat-file", "blob", out.split()[2]).stdout.decode().strip()
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), target))


SKIPPED = ("__pycache__",)
COPIED = {
    "skills": (
        "merge-drain/scripts/*.py",
        "merge-drain/scripts/*.json",
        "merge-drain/landing_decision.pin.json",
        "merge-drain/SKILL.md",
        "merge-sweep/*.json",
        "queue-status/scripts/*.py",
        "arm-green/scripts/*.py",
        "release-cut/scripts/*.sh",
        "release-cut/scripts/*.py",
        "lab-rebuild-triage/scripts/*.sh",
    ),
    "scripts": ("*.py",),
    "src": ("**/*",),
}


def tree_differences(
    repo: Path, commit: str, prefix: str, install_dir: Path
) -> tuple[int, list[str]]:
    """``(files compared, files that differ)`` between the installed controller tree and ``commit``.

    The installer copies ``<prefix>`` to ``skills/``, ``<prefix>/../scripts`` to ``scripts/`` and
    ``<prefix>/../src`` to ``src/``. A file the commit does not have is not compared, nor one no
    install copies.
    """
    plugin = posixpath.dirname(prefix.rstrip("/"))
    parts = {
        "skills": prefix,
        "scripts": posixpath.join(plugin, "scripts"),
        "src": linked_dir(repo, commit, posixpath.join(plugin, "src")),
    }
    compared, differ = (0, [])
    for part, in_repo in parts.items():
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
