# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Production ports of the rotation tick: the host's login file and the source host's status mirror (OMN-23000)."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_status_view import (
    ModelPoolHostLogin,
)
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


def remote_command(target: str, script: str) -> list[str]:
    """The one place a pool port reaches the marks source's host: ``script`` run there by the address the overlay names."""
    return ["ssh", *SSH_OPTS, target, script]


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

    def since(self) -> datetime | None:
        """When the login now on the host was written, ``None`` when there is no login."""
        try:
            return datetime.fromtimestamp(self._path.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None

    @staticmethod
    def _private_handle(path: Path) -> TextIO:
        """Open ``path`` for writing with mode 0600 set before any secret is written.

        ``os.open`` applies its mode only when it creates the file, so an existing, more permissive
        file is narrowed on the open descriptor first.
        """
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")

    def write(self, value: SecretStr) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            prev = self._path.with_name(self._path.name + ".prev")
            with self._private_handle(prev) as handle:
                handle.write(self._path.read_text(encoding="utf-8"))
        tmp = self._path.with_name(self._path.name + ".new")
        with self._private_handle(tmp) as handle:
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
        done = self._run(remote_command(self._target, f'cat "$HOME/{self._mirror}"'))
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
                    fingerprint=row.get("fingerprint"),
                    revoked_at=row.get("revoked_at"),
                    limited_at=row.get("limited_at"),
                    revoke_cause=row.get("revoke_cause"),
                    probed_at=row.get("probed_at"),
                )
                for row in body["tokens"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            return ModelRotationMarksRead(
                state="unreadable", detail=f"mirror not readable: {type(exc).__name__}"
            )
        return ModelRotationMarksRead(state="read", at=at, marks=marks)


class DirectoryLoginBoard:
    """The login board: one small JSON record per host in a directory beside the marks source's mirror.

    ``ssh_target`` ``None`` means the directory is on this host (the marks source itself); otherwise every
    read and write is run on the source host, by the address the overlay already dials it by. A record is
    ``{"host", "label", "at"}``: a label, never a token or a fingerprint.
    """

    def __init__(
        self,
        *,
        home: Path,
        directory: str,
        ssh_target: str | None,
        run: Runner = _run,
    ) -> None:
        self._home = home
        self._directory = directory
        self._target = ssh_target
        self._run = run

    def publish(self, login: ModelPoolHostLogin) -> None:
        record = json.dumps(
            {
                "host": login.host,
                "label": login.label,
                "at": login.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            sort_keys=True,
        )
        if self._target is None:
            directory = self._home / self._directory
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{login.host}.json"
            temporary = directory / f".{login.host}.json.new"
            temporary.write_text(record + "\n", encoding="utf-8")
            os.replace(temporary, path)
            return
        remote = f"$HOME/{self._directory}"
        name = shlex.quote(login.host + ".json")
        script = (
            f'mkdir -p "{remote}" && cd "{remote}" && '
            f"printf '%s\\n' {shlex.quote(record)} > .{name}.new && mv .{name}.new {name}"
        )
        done = self._run(remote_command(self._target, script))
        if done.returncode:
            raise OSError(f"login board write: remote exit {done.returncode}")

    def read(self) -> tuple[ModelPoolHostLogin, ...] | None:
        if self._target is None:
            directory = self._home / self._directory
            try:
                texts = [
                    path.read_text(encoding="utf-8")
                    for path in sorted(directory.glob("*.json"))
                ]
            except OSError:
                return None
        else:
            script = (
                f'cd "$HOME/{self._directory}" && '
                'for f in *.json; do [ -e "$f" ] && cat "$f"; done'
            )
            done = self._run(remote_command(self._target, script))
            if done.returncode:
                return None
            texts = done.stdout.splitlines()
        logins: list[ModelPoolHostLogin] = []
        for text in texts:
            if not text.strip():
                continue
            try:
                body = json.loads(text)
                logins.append(
                    ModelPoolHostLogin(
                        host=body["host"],
                        label=body.get("label"),
                        at=datetime.strptime(body["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=UTC
                        ),
                    )
                )
            except (ValueError, KeyError, TypeError):
                # a record that does not parse places no host: its host reads as unread
                continue
        return tuple(logins)
