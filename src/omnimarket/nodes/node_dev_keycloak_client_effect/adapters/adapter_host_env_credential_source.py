# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bootstrap credential source: the host env file's wanted names, each value carried as a secret.

Nothing is ever logged: a name or a missing file is the only fact this adapter answers with.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr


class HostEnvCredentialSource:
    """``ProtocolBootstrapCredentialSource`` over one host env file.

    ``read(names)`` answers only the wanted names the file holds. A missing or unreadable file
    answers with nothing. Each ``NAME=value`` line may carry an ``export `` prefix and quoted
    values; the quotes are stripped.
    """

    def __init__(self, env_file: Path) -> None:
        self._env_file = env_file

    async def read(self, names: Sequence[str]) -> dict[str, SecretStr]:
        wanted = set(names)
        found: dict[str, SecretStr] = {}
        try:
            lines = self._env_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return found
        for line in lines:
            name, sep, raw = line.strip().removeprefix("export ").partition("=")
            name = name.strip()
            if not sep or name not in wanted:
                continue
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value.strip():
                found[name] = SecretStr(value)
        return found


__all__ = ["HostEnvCredentialSource"]
