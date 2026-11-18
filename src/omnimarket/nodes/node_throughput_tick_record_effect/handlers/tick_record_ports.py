# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record step's state stores: the state directory on disk, and an in-memory one (OMN-23734)."""

from __future__ import annotations

import os
from pathlib import Path


class FileTickStateStore:
    """The tick's per-user state directory. A write goes to a scratch file named by the pid and is renamed over the row."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, name: str) -> str:
        return str(self._directory / name)

    def write_atomic(self, name: str, text: str) -> None:
        target = self._directory / name
        self._directory.mkdir(parents=True, exist_ok=True)
        scratch = target.with_name(f"{target.name}.{os.getpid()}")
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, target)

    def append_text(self, name: str, text: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / name).open("a", encoding="utf-8") as log:
            log.write(text)


class InMemoryTickStateStore:
    """Rows held in a dict. ``fail`` names the rows whose write raises ``OSError`` with that text (for tests)."""

    def __init__(
        self, directory: str = "/state", fail: dict[str, str] | None = None
    ) -> None:
        self.directory = directory
        self.rows: dict[str, str] = {}
        self._fail = fail or {}

    def path(self, name: str) -> str:
        return f"{self.directory}/{name}"

    def write_atomic(self, name: str, text: str) -> None:
        if name in self._fail:
            raise OSError(self._fail[name])
        self.rows[name] = text

    def append_text(self, name: str, text: str) -> None:
        if name in self._fail:
            raise OSError(self._fail[name])
        self.rows[name] = self.rows.get(name, "") + text


__all__ = ["FileTickStateStore", "InMemoryTickStateStore"]
