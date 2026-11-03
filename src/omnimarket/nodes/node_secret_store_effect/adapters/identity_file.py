# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ``NAME=value`` file as the bootstrap store for a machine's identity (OMN-21921).

A lab host already keeps its store identity in one such file under the user's
home, so the overlay can name that file instead of a second copy in
``credentials.json``. The file is read in process for exactly the names the
overlay asks for and is refused unless owner-only, on every read: a file
survives ``chmod``, backup and ``scp``. Nothing here logs or returns a name
other than the one asked for.
"""

from __future__ import annotations

import stat
from pathlib import Path

#: The shapes of message the node's handler lets through into a ``detail``.
_MODE_REFUSAL = "identity refused: the identity file must be mode 0600"


class IdentityFileStore:
    """Read-only ``ProtocolSecretStore`` over a ``NAME=value`` file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _values(self) -> dict[str, str]:
        mode = stat.S_IMODE(self._path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(_MODE_REFUSAL)
        found: dict[str, str] = {}
        for line in self._path.read_text(encoding="utf-8").splitlines():
            name, sep, raw = line.strip().removeprefix("export ").partition("=")
            if sep and not name.startswith("#"):
                found[name.strip()] = raw.strip().strip("\"'")
        return found

    async def get_secret(self, key: str) -> str | None:
        return self._values().get(key) or None

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("the identity file is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("the identity file is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
