# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recoverable bounded profile edits; never source, evaluate or execute profiles."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_shell import (
    ModelSetupProfileSnapshot,
)


def read_profile(path: Path) -> ModelSetupProfileSnapshot:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Profile must be a bounded regular file")
        content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Profile grew during inspection")
    content.decode("utf-8")
    return ModelSetupProfileSnapshot(
        path=path,
        content=content,
        mode=stat.S_IMODE(info.st_mode),
        device=info.st_dev,
        inode=info.st_ino,
    )


def validate_syntax(content: bytes, shell: str) -> None:
    flags = ["--noprofile", "--norc", "-n"] if shell == "/bin/bash" else ["-f", "-n"]
    result = subprocess.run(
        [shell, *flags],
        input=content,
        capture_output=True,
        timeout=10,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "ZDOTDIR": "/var/empty"},
    )
    if result.returncode:
        raise ValueError("Profile syntax cannot be safely preserved")


def _unchanged(snapshot: ModelSetupProfileSnapshot) -> None:
    if read_profile(snapshot.path) != snapshot:
        raise ValueError("Profile changed after inspection")


def _atomic(path: Path, content: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".onex-profile-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), mode)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_profiles(
    edits: list[tuple[ModelSetupProfileSnapshot, tuple[int, ...], str]],
    *,
    home: Path,
    marker: str,
) -> tuple[str, ...]:
    """Prepare and parse every candidate before backing up or changing any profile."""
    prepared: list[tuple[ModelSetupProfileSnapshot, bytes, str]] = []
    for snapshot, lines, shell in edits:
        digest = hashlib.sha256(snapshot.content).hexdigest()
        backup_name = (
            snapshot.path.name + "." + digest[:16] + "." + uuid4().hex + ".backup"
        )
        original = snapshot.content.decode("utf-8").splitlines(keepends=True)
        replacement = "".join(
            marker + backup_name + ":" + str(index) + "\n" if index in lines else line
            for index, line in enumerate(original, 1)
        ).encode()
        validate_syntax(snapshot.content, shell)
        validate_syntax(replacement, shell)
        prepared.append((snapshot, replacement, backup_name))
    for snapshot, _, _ in prepared:
        _unchanged(snapshot)
    backup_dir = home / "profile-backups"
    if home.is_symlink() or backup_dir.is_symlink():
        raise ValueError("Recovery directories must not be symbolic links")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    references: list[str] = []
    for snapshot, replacement, backup_name in prepared:
        _unchanged(snapshot)
        backup = backup_dir / backup_name
        _atomic(backup, snapshot.content, 0o600)
        _unchanged(snapshot)
        _atomic(snapshot.path, replacement, snapshot.mode)
        references.append(str(backup))
    return tuple(references)
