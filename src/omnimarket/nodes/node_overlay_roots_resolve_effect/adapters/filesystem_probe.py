# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem implementation of the overlay root probe (OMN-24032)."""

from __future__ import annotations

from pathlib import Path


class FileSystemOverlayRootsProbe:
    def is_dir(self, path: str) -> bool:
        return Path(path).is_dir()

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def resolve(self, path: str) -> str:
        return str(Path(path).resolve())
