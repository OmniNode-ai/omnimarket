# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe port: every read the collector makes goes through it."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Protocol


class ProbeError(Exception):
    """A source could not be read."""


class ProtocolLabSliProbe(Protocol):
    def system(self) -> str: ...

    def read_text(self, path: str) -> str: ...

    def run(self, argv: tuple[str, ...]) -> str: ...

    def disk_free_bytes(self, path: str) -> int: ...

    def tree_bytes(self, path: str) -> int: ...


class LocalLabSliProbe:
    """The host the effect runs on."""

    def system(self) -> str:
        return platform.system()

    def read_text(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProbeError(f"{path}: {exc.strerror or exc}") from exc

    def run(self, argv: tuple[str, ...]) -> str:
        try:
            done = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"{argv[0]}: {exc}") from exc
        if done.returncode != 0:
            raise ProbeError(
                f"{argv[0]}: exit {done.returncode}: {done.stderr.strip()[:200]}"
            )
        return done.stdout

    def disk_free_bytes(self, path: str) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError as exc:
            raise ProbeError(f"{path}: {exc.strerror or exc}") from exc

    def tree_bytes(self, path: str) -> int:
        if not os.path.isdir(path):
            raise ProbeError(f"{path}: not a directory")
        total = 0
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError as exc:
            raise ProbeError(f"{path}: {exc.strerror or exc}") from exc
        return total


__all__ = ["LocalLabSliProbe", "ProbeError", "ProtocolLabSliProbe"]
