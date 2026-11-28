# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three Valkey logins of the settings store, as the access-list commands the bootstrap applies.

Writer: reads and writes the store's key prefix, used by the lane runtime's Valkey writer alone. Reader: reads that
prefix only, fetched by the hosts. Cache: the existing cache login, narrowed so it can no longer touch the prefix
(it keeps no key pattern of the store). The names and the prefix come from the wiring; none is written here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

# +select: the store's client opens its numbered database with SELECT before any command, so a login without it authenticates and
# is then refused everything (measured on the dev Valkey, OMN-24052). The key pattern still bounds what the login can touch.
READ_COMMANDS = (
    "+get",
    "+mget",
    "+exists",
    "+ttl",
    "+type",
    "+scan",
    "+ping",
    "+select",
)
WRITE_COMMANDS = ("+set", "+del", "+expire", "+persist")


class ModelValkeyStoreLogins(BaseModel):
    """The prefix and the three login names, all given by the bootstrap inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_prefix: str = Field(min_length=1)
    writer_login: str = Field(min_length=1)
    reader_login: str = Field(min_length=1)
    cache_login: str = Field(min_length=1)
    cache_key_pattern: str = Field(min_length=1)


def render_store_acl_commands(
    logins: ModelValkeyStoreLogins, *, passwords: Mapping[str, str]
) -> list[tuple[str, ...]]:
    """One ``ACL SETUSER`` argument tuple per login. Every login starts from ``reset``, so it holds nothing it is not given."""
    scope = f"{logins.key_prefix}:*"
    return [
        _setuser(
            logins.writer_login,
            passwords,
            f"%RW~{scope}",
            *READ_COMMANDS,
            *WRITE_COMMANDS,
        ),
        _setuser(logins.reader_login, passwords, f"%R~{scope}", *READ_COMMANDS),
        # The cache login keeps the commands a cache needs, on the cache's own key pattern only: it names no key
        # pattern of the store, so a read or a write of a settings key is refused.
        _setuser(
            logins.cache_login,
            passwords,
            f"~{logins.cache_key_pattern}",
            "-@all",
            *READ_COMMANDS,
            *WRITE_COMMANDS,
        ),
    ]


def render_login_acl_command(
    logins: ModelValkeyStoreLogins, role: str, *, password: str
) -> tuple[str, ...]:
    """The one ``ACL SETUSER`` row of a role (``writer``, ``reader`` or ``cache``), given that login's password alone."""
    names = {
        "writer": logins.writer_login,
        "reader": logins.reader_login,
        "cache": logins.cache_login,
    }
    login = names[role]
    placeholders = dict.fromkeys(names.values(), "")
    placeholders[login] = password
    rows = dict(
        zip(
            names,
            render_store_acl_commands(logins, passwords=placeholders),
            strict=True,
        )
    )
    return rows[role]


def _setuser(login: str, passwords: Mapping[str, str], *rules: str) -> tuple[str, ...]:
    return ("ACL", "SETUSER", login, "reset", "on", f">{passwords[login]}", *rules)


__all__ = [
    "ModelValkeyStoreLogins",
    "render_login_acl_command",
    "render_store_acl_commands",
]
