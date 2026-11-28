# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey seam the provision node applies access-list rows through (OMN-24052). One admin connection, no value returned."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


class ValkeyUnavailableError(Exception):
    """The Valkey could not be reached or refused the admin login; the message is the error's type name only."""


@runtime_checkable
class ProtocolValkeyAclApplier(Protocol):
    """Read the keyspace, apply one ``ACL SETUSER`` row, and check that a login authenticates."""

    async def keyspace_counts(self) -> Mapping[int, int]: ...

    async def user_exists(self, login: str) -> bool: ...

    async def set_user(self, command: tuple[str, ...]) -> None: ...

    async def authenticates(self, login: str, password: str) -> bool:
        """A session on the store's database opens and answers PING: the password is right and the row lets it in."""
        ...

    async def accepts_password(self, login: str, password: str) -> bool:
        """The password alone is accepted (a session on the default database), whatever the login's row holds."""
        ...


__all__ = ["ProtocolValkeyAclApplier", "ValkeyUnavailableError"]
