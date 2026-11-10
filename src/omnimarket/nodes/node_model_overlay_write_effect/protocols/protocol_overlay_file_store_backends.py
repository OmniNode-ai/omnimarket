# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two overlay file stores: in memory (the default) and on disk (OMN-20330)."""

from __future__ import annotations

from pathlib import Path


class InMemoryOverlayFileStore:
    """Holds the overlay text in a dict; touches no disk."""

    def __init__(self) -> None:
        self.files: dict[Path, str] = {}

    def read_text(self, path: Path) -> str | None:
        return self.files.get(path)

    def write_text(self, path: Path, text: str) -> None:
        self.files[path] = text


class FilesystemOverlayFileStore:
    """The real backend: a temp file beside the target, then an atomic replace."""

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)


__all__ = ["FilesystemOverlayFileStore", "InMemoryOverlayFileStore"]
