# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the repository source: in memory, and git objects at a ref.

The git backend reads committed objects only (``ls-tree`` and ``cat-file --batch``), so it never
touches a working tree and reads the exact head the ref names, whatever is checked out.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_code_graph import ModelRepoSnapshot
from omnimarket.nodes.node_code_graph_effect.protocols import RepoSourceError


class InMemoryRepoSource:
    """Repositories as ``{repo: {ref: (head, {path: text})}}``; a missing one raises."""

    def __init__(
        self, repos: Mapping[str, Mapping[str, tuple[str, Mapping[str, str]]]]
    ) -> None:
        self._repos = repos

    def snapshot(
        self, repo: str, ref: str, wanted: Callable[[str], bool]
    ) -> ModelRepoSnapshot:
        refs = self._repos.get(repo)
        if refs is None or ref not in refs:
            raise RepoSourceError(f"no-repository:{repo}@{ref}")
        head, files = refs[ref]
        return ModelRepoSnapshot(
            repo=repo,
            ref=ref,
            head=head,
            files={path: text for path, text in files.items() if wanted(path)},
        )


class GitRepoSource:
    """Read ``<root>/<repo>`` at a ref from its git objects."""

    def __init__(
        self, root: Path, *, max_file_bytes: int, timeout_seconds: int = 600
    ) -> None:
        self._root = root
        self._max = max_file_bytes
        self._timeout = timeout_seconds

    def _git(self, clone: Path, *arguments: str, stdin: bytes | None = None) -> bytes:
        try:
            done = subprocess.run(
                ["git", "-C", str(clone), *arguments],
                input=stdin,
                capture_output=True,
                check=False,
                timeout=self._timeout,
                env=scrub_git_location_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoSourceError(f"git-{type(exc).__name__}:{clone.name}") from None
        if done.returncode:
            first = done.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
            raise RepoSourceError(
                f"git-{arguments[0]}-exit-{done.returncode}:{clone.name}:{''.join(first)[:160]}"
            )
        return done.stdout

    def committed_at(self, repo: str, commit: str) -> datetime | None:
        """The commit's time (the lookup's commit clock); none when the clone does not hold it."""
        clone = self._root / repo
        if not (clone / ".git").exists():
            return None
        try:
            stamp = self._git(
                clone, "show", "-s", "--format=%cI", f"{commit}^{{commit}}"
            )
            return datetime.fromisoformat(stamp.decode().strip())
        except (RepoSourceError, ValueError):
            return None

    def snapshot(
        self, repo: str, ref: str, wanted: Callable[[str], bool]
    ) -> ModelRepoSnapshot:
        clone = self._root / repo
        if not (clone / ".git").exists():
            raise RepoSourceError(f"no-clone:{repo}")
        head = (
            self._git(clone, "rev-parse", "--verify", f"{ref}^{{commit}}")
            .decode()
            .strip()
        )
        listing = self._git(clone, "ls-tree", "-r", "-z", "--long", head)
        paths: list[str] = []
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, name = entry.partition(b"\t")
            parts = meta.split()
            if len(parts) < 4 or parts[1] != b"blob" or parts[3] == b"-":
                continue
            path = name.decode("utf-8", "replace")
            if int(parts[3]) <= self._max and wanted(path):
                paths.append(path)
        files: dict[str, str] = {}
        if paths:
            request = "".join(f"{head}:{path}\n" for path in paths).encode("utf-8")
            files = _parse_batch(
                self._git(clone, "cat-file", "--batch", stdin=request), paths
            )
        return ModelRepoSnapshot(repo=repo, ref=ref, head=head, files=files)


def _parse_batch(out: bytes, paths: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    position = 0
    for path in paths:
        end = out.index(b"\n", position)
        header = out[position:end].split()
        position = end + 1
        if len(header) != 3 or header[1] != b"blob":
            raise RepoSourceError(f"cat-file-missing:{path}")
        size = int(header[2])
        files[path] = out[position : position + size].decode("utf-8", "replace")
        position += size + 1
    return files
