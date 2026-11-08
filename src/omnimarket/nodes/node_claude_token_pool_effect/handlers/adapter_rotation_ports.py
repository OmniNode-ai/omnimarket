# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production ports of the rotation tick: the host's login file and the source host's status mirror (OMN-23000)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMark,
    ModelRotationMarksRead,
)

SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8")
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), capture_output=True, text=True, check=False, timeout=30
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 127, "", type(exc).__name__)


class FileTokenFile:
    """The login file of this host, written atomically with mode 0600 and the old file kept as ``.prev``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def fingerprint(self) -> str | None:
        try:
            value = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] if value else None

    def write(self, value: SecretStr) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            prev = self._path.with_name(self._path.name + ".prev")
            fd = os.open(prev, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._path.read_text(encoding="utf-8"))
        tmp = self._path.with_name(self._path.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value.get_secret_value() + "\n")
        os.replace(tmp, self._path)


class MirrorMarksSource:
    """Reads the source host's pool status mirror: in place, or over ssh when the source is another host."""

    def __init__(
        self,
        *,
        home: Path,
        mirror_path: str,
        ssh_target: str | None,
        run: Runner = _run,
    ) -> None:
        self._home = home
        self._mirror = mirror_path
        self._target = ssh_target
        self._run = run

    def _text(self) -> tuple[str | None, str]:
        if self._target is None:
            try:
                return (self._home / self._mirror).read_text(encoding="utf-8"), ""
            except OSError as exc:
                return None, type(exc).__name__
        done = self._run(
            ["ssh", *SSH_OPTS, self._target, f'cat "$HOME/{self._mirror}"']
        )
        if done.returncode:
            return None, f"ssh exit {done.returncode}"
        return done.stdout, ""

    def read(self) -> ModelRotationMarksRead:
        text, why = self._text()
        if text is None:
            return ModelRotationMarksRead(state="unreadable", detail=why)
        try:
            body = json.loads(text)
            at = datetime.strptime(body["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            marks = tuple(
                ModelRotationMark(
                    label=row["label"],
                    until=row.get("until"),
                    revoked=bool(row.get("revoked")),
                )
                for row in body["tokens"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            return ModelRotationMarksRead(
                state="unreadable", detail=f"mirror not readable: {type(exc).__name__}"
            )
        return ModelRotationMarksRead(state="read", at=at, marks=marks)
