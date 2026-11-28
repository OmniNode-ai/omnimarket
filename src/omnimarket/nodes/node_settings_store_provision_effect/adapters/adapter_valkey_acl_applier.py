# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production Valkey applier: one admin connection built from the overlay's values and a password the handler read."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import SecretStr

from omnimarket.nodes.node_settings_store_provision_effect.protocols.protocol_valkey_acl_applier import (
    ValkeyUnavailableError,
)


def _redis_client(
    *, host: str, port: int, database: int, username: str, password: str
) -> Any:
    from redis.asyncio import Redis

    return Redis(
        host=host,
        port=port,
        db=database,
        username=username,
        password=password,
        decode_responses=True,
        socket_connect_timeout=5.0,
        socket_timeout=10.0,
    )


class ValkeyAclApplier:
    """``ProtocolValkeyAclApplier`` over redis.asyncio; an error is reported by its type name alone, never its text."""

    def __init__(
        self,
        *,
        address: str,
        port: int,
        database: int,
        admin_login: str,
        admin_password: SecretStr,
        client_factory: Callable[..., Any] = _redis_client,
    ) -> None:
        self._address = address
        self._port = port
        self._database = database
        self._admin_login = admin_login
        self._admin_password = admin_password
        self._client_factory = client_factory

    def _client(self, login: str, password: str, *, database: int | None = None) -> Any:
        return self._client_factory(
            host=self._address,
            port=self._port,
            database=self._database if database is None else database,
            username=login,
            password=password,
        )

    def _admin(self) -> Any:
        return self._client(self._admin_login, self._admin_password.get_secret_value())

    async def keyspace_counts(self) -> Mapping[int, int]:
        client = self._admin()
        try:
            info = await client.info("keyspace")
        except Exception as exc:
            raise ValkeyUnavailableError(type(exc).__name__) from None
        finally:
            await client.aclose()
        counts: dict[int, int] = {}
        for name, fields in info.items():
            if name.startswith("db") and name[2:].isdigit():
                counts[int(name[2:])] = int(fields["keys"])
        return counts

    async def user_exists(self, login: str) -> bool:
        client = self._admin()
        try:
            names = await client.execute_command("ACL", "USERS")
        except Exception as exc:
            raise ValkeyUnavailableError(type(exc).__name__) from None
        finally:
            await client.aclose()
        return login in names

    async def set_user(self, command: tuple[str, ...]) -> None:
        client = self._admin()
        try:
            await client.execute_command(*command)
        except Exception as exc:
            raise ValkeyUnavailableError(type(exc).__name__) from None
        finally:
            await client.aclose()

    async def accepts_password(self, login: str, password: str) -> bool:
        client = self._client(login, password, database=0)
        try:
            return bool(await client.ping())
        except Exception:
            return False
        finally:
            await client.aclose()

    async def authenticates(self, login: str, password: str) -> bool:
        client = self._client(login, password)
        try:
            return bool(await client.ping())
        except Exception:
            return False
        finally:
            await client.aclose()


__all__ = ["ValkeyAclApplier"]
