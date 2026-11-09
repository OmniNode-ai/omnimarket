# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file list a commit installs, read and compared with an install's copy (OMN-22955).

Shared by the install superset guard and the install canary, which both ask which files an install should hold and which
of them its copy lacks. It lives here, not in either node, so neither node imports the other's private handler.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

GIT_TIMEOUT_S = 120


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
            env=env if env is not None else scrub_git_location_env(os.environ),
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


def pattern_matches(pattern: str, rel: str) -> bool:
    """True when ``rel`` is a path an install copies under ``pattern``: ``*`` stays in one directory, ``**/*`` goes deep."""
    regex = re.escape(pattern).replace("\\*\\*/\\*", ".*").replace("\\*", "[^/]*")
    return re.fullmatch(regex, rel) is not None


def install_parts(repo: Path, commit: str, prefix: str) -> dict[str, str]:
    """``{install directory: repository path}`` for the three trees the installer copies."""
    plugin = posixpath.dirname(prefix.rstrip("/"))
    return {
        "skills": prefix,
        "scripts": posixpath.join(plugin, "scripts"),
        "src": linked_dir(repo, commit, posixpath.join(plugin, "src")),
    }


def file_list_comparison(
    repo: Path, commit: str, prefix: str, install_dir: Path
) -> tuple[int, list[str]]:
    """``(files the commit holds that an install copies, those the install's copy lacks)``.

    ``tree_differences`` reads the install's own files, so a root data file the copy lost is never
    compared. This reads the commit's file list instead: each file the installer copies that is
    absent from ``install_dir`` is named, so a whole tree the copy lacks names every file of it. A tree the
    commit does not have (no ``src/``) holds no file to lose.
    """
    expected, missing = (0, [])
    for part, in_repo in install_parts(repo, commit, prefix).items():
        root = install_dir / part
        for rel, (mode, _blob) in sorted(ls_tree(repo, commit, in_repo).items()):
            if mode == "120000" or any(s in rel.split("/") for s in SKIPPED):
                continue
            if not any(pattern_matches(pat, rel) for pat in COPIED[part]):
                continue
            expected += 1
            if not (root / rel).is_file():
                missing.append(f"{part}/{rel}")
    return (expected, missing)
