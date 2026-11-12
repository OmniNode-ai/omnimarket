# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key-value store of the host table keys (OMN-23983)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import redis

from ..models.model_host_table_keys import ModelHostTableKeysOverlay


class ProtocolHostKeyStore(Protocol):
    """The synchronous key-value store the ingest and read effects share."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


class ProtocolKeyValueClient(Protocol):
    """What the Valkey store needs of a client: redis.Redis built with decode_responses, or a double."""

    def get(self, name: str) -> str | bytes | None: ...

    def set(self, name: str, value: str) -> object: ...


class StoreHostKeysInMemory:
    """A dict-backed store: the default profile and the test double."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value


class HostTableKeysStoreUnconfiguredError(RuntimeError):
    """A variable the overlay names is missing from the environment."""


class StoreHostKeysValkey:
    """A redis-backed store: one key, written whole, so a reader never sees half a table."""

    def __init__(self, client: ProtocolKeyValueClient) -> None:
        self._client = client

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        if value is None:
            return None
        return value if isinstance(value, str) else value.decode("utf-8")

    def set(self, key: str, value: str) -> None:
        self._client.set(key, value)

    @classmethod
    def from_environment(
        cls, overlay: ModelHostTableKeysOverlay, environ: Mapping[str, str]
    ) -> StoreHostKeysValkey:
        host = environ.get(overlay.valkey_host_env)
        if host is None:
            raise HostTableKeysStoreUnconfiguredError(
                f"the variable {overlay.valkey_host_env} is not set"
            )
        port = environ.get(overlay.valkey_port_env)
        if port is None:
            raise HostTableKeysStoreUnconfiguredError(
                f"the variable {overlay.valkey_port_env} is not set"
            )
        password = environ.get(overlay.valkey_password_env)
        if password is None:
            raise HostTableKeysStoreUnconfiguredError(
                f"the variable {overlay.valkey_password_env} is not set"
            )
        client = redis.Redis(
            host=host,
            port=int(port),
            password=password,
            socket_timeout=overlay.connect_timeout_seconds,
            socket_connect_timeout=overlay.connect_timeout_seconds,
            decode_responses=True,
        )
        return cls(client)
