# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File ports of node_host_reading_effect: in memory by default, the local filesystem by the CLI."""

from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path

from omnimarket.nodes.node_host_reading_effect.protocols import PathKind


class LocalHostFiles:
    """The host's own filesystem, read only."""

    def kind(self, path: Path) -> PathKind:
        if path.is_symlink():
            return "link"
        if not path.exists():
            return "missing"
        return "directory" if path.is_dir() else "file"

    def resolve(self, path: Path) -> Path:
        return Path(os.path.realpath(path))

    def list_names(self, directory: Path, pattern: str) -> tuple[str, ...]:
        if not directory.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in directory.iterdir()
                if fnmatchcase(entry.name, pattern)
            )
        )

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")


class InMemoryHostFiles:
    """The same port over dicts. ``read_error`` injects a failed read."""

    def __init__(self) -> None:
        self.links: dict[Path, Path] = {}
        self.files: dict[Path, str] = {}
        self.directories: set[Path] = set()
        self.read_error: str | None = None

    def add_file(self, path: Path, text: str = "") -> None:
        self.files[path] = text
        for parent in path.parents:
            self.directories.add(parent)

    def kind(self, path: Path) -> PathKind:
        self._check()
        if path in self.links:
            return "link"
        if path in self.files:
            return "file"
        return "directory" if path in self.directories else "missing"

    def resolve(self, path: Path) -> Path:
        self._check()
        seen = set()
        while path in self.links and path not in seen:
            seen.add(path)
            path = self.links[path]
        return path

    def list_names(self, directory: Path, pattern: str) -> tuple[str, ...]:
        self._check()
        entries = [*self.files, *self.directories, *self.links]
        return tuple(
            sorted(
                {
                    entry.name
                    for entry in entries
                    if entry.parent == directory and fnmatchcase(entry.name, pattern)
                }
            )
        )

    def read_text(self, path: Path) -> str:
        self._check()
        try:
            return self.files[path]
        except KeyError as exc:
            raise FileNotFoundError(str(path)) from exc

    def _check(self) -> None:
        if self.read_error is not None:
            raise OSError(self.read_error)
