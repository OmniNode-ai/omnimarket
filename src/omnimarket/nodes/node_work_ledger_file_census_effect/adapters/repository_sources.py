# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Repository sources and the ratchet store of the no-file census."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)


class InMemoryRepositorySource:
    """Repositories as ``{name: {path: text}}``. The default backend of every suite."""

    def __init__(self, repositories: Mapping[str, Mapping[str, str]]) -> None:
        self._repositories = {k: dict(v) for k, v in repositories.items()}

    def present(self, repository: str) -> bool:
        return repository in self._repositories

    def candidates(self, repository: str, tokens: Sequence[str]) -> tuple[str, ...]:
        return tuple(self._repositories[repository])

    def read(self, repository: str, path: str) -> str | None:
        return self._repositories[repository][path]


class GitRepositorySource:
    """The tracked files of clones directly under a registry root, read from their working trees."""

    def __init__(
        self,
        root: Path,
        max_file_bytes: int,
        directory_of: Mapping[str, str] | None = None,
    ) -> None:
        self._root = root
        self._max = max_file_bytes
        self._directory_of = dict(directory_of or {})

    def _dir(self, repository: str) -> Path:
        return self._root / self._directory_of.get(repository, repository)

    def _git(self, repository: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self._dir(repository)), *args],
            check=False,
            capture_output=True,
            text=True,
            env=scrub_git_location_env(os.environ),
        )

    def present(self, repository: str) -> bool:
        return (self._dir(repository) / ".git").exists()

    def candidates(self, repository: str, tokens: Sequence[str]) -> tuple[str, ...]:
        args = ["grep", "-l", "-z", "-I", "-F", "--no-recurse-submodules"]
        for token in tokens:
            args += ["-e", token]
        done = self._git(repository, *args)
        if done.returncode not in (0, 1):
            raise OSError(f"git grep failed in {repository}: exit {done.returncode}")
        return tuple(name for name in done.stdout.split("\0") if name)

    def read(self, repository: str, path: str) -> str | None:
        target = self._dir(repository) / path
        if not target.is_file() or target.stat().st_size > self._max:
            return None
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None


class YamlRatchetStore:
    """``{repository: files}`` in a YAML mapping under the key ``ratchet``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict[str, int]:
        data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        table = data.get("ratchet") if isinstance(data, dict) else None
        if not isinstance(table, dict) or not all(
            isinstance(v, int) and v >= 0 for v in table.values()
        ):
            raise ValueError("ratchet file has no table of counts")
        return {str(k): v for k, v in table.items()}

    def write(self, table: Mapping[str, int], header: str) -> None:
        """Merge with the row values now on disk, keeping the lower of each, then replace the file whole.

        A run that read the table before another run lowered a row cannot raise that row back, and a
        write that dies midway leaves the earlier file in place.
        """
        merged = dict(table)
        try:
            current = self.read()
        except (OSError, ValueError, yaml.YAMLError):
            current = {}
        for repository, files in current.items():
            merged[repository] = min(files, merged.get(repository, files))
        data: dict[str, object] = {"ratchet": dict(sorted(merged.items()))}
        try:
            rows = yaml.safe_load(self._path.read_text(encoding="utf-8")).get("files")
        except (OSError, yaml.YAMLError, AttributeError):
            rows = None
        if rows:
            data["files"] = rows
        body = yaml.safe_dump(data, sort_keys=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(header + body, encoding="utf-8")
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


class InMemoryRatchetStore:
    def __init__(self, table: Mapping[str, int] | None) -> None:
        self._table = None if table is None else dict(table)

    def read(self) -> dict[str, int]:
        if self._table is None:
            raise OSError("ratchet file missing")
        return dict(self._table)


class YamlFileBaselineStore:
    """``{repository: {path: hits}}`` in the ``files`` table of the ratchet file, beside its ``ratchet`` table."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict[str, dict[str, int]]:
        data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        table = data.get("files") if isinstance(data, dict) else None
        if not isinstance(table, dict) or not all(
            isinstance(rows, dict)
            and all(isinstance(v, int) and v > 0 for v in rows.values())
            for rows in table.values()
        ):
            raise ValueError("ratchet file has no table of per-file counts")
        return {
            str(repo): {str(path): hits for path, hits in rows.items()}
            for repo, rows in table.items()
        }

    def write(self, table: Mapping[str, Mapping[str, int]], header: str) -> None:
        """Lower, never raise: a row is the lower of the table's and the file's, and a file the table lacks is dropped.

        A repository the table does not name was not counted by this run: its rows stay as they are.

        A path the file on disk lacks is never added, so a run that read the table before another run
        lowered it cannot raise a row back. The ``ratchet`` table is kept as it is. The file is replaced
        whole, so a write that dies midway leaves the earlier file in place.
        """
        current = self.read()
        merged: dict[str, dict[str, int]] = {}
        for repository, rows in current.items():
            if repository not in table:
                merged[repository] = dict(sorted(rows.items()))
                continue
            kept = table[repository]
            merged[repository] = {
                path: min(hits, kept[path])
                for path, hits in sorted(rows.items())
                if path in kept and kept[path] > 0
            }
        counts = yaml.safe_load(self._path.read_text(encoding="utf-8")).get(
            "ratchet", {}
        )
        body = yaml.safe_dump({"files": merged, "ratchet": counts}, sort_keys=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(header + body, encoding="utf-8")
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


class InMemoryFileBaselineStore:
    def __init__(self, table: Mapping[str, Mapping[str, int]] | None) -> None:
        self._table = (
            None
            if table is None
            else {repo: dict(rows) for repo, rows in table.items()}
        )

    def read(self) -> dict[str, dict[str, int]]:
        if self._table is None:
            raise OSError("baseline file missing")
        return {repo: dict(rows) for repo, rows in self._table.items()}
