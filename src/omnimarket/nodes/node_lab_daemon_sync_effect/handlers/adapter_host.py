# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real host boundary: subprocesses, JSON reads, non-blocking lock probes, atomic writes."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path


class LabSyncHostIo:
    def run(self, argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return 124, "", f"timed out after {timeout_s}s: {exc}"
        return done.returncode, done.stdout, done.stderr

    def read_json(self, path: str) -> dict[str, object] | None:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def lock_busy(self, path: str) -> bool:
        """Whether another process holds the flock; an absent lock file is free."""
        if not Path(path).is_file():
            return False
        with Path(path).open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False

    def write_text(self, path: str, text: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)

    def exists(self, path: str) -> bool:
        return Path(path).exists()
