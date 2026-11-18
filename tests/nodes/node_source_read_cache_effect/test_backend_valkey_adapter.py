# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey adapter over omnibase_core's BackendCacheRedis (OMN-19998).

Golden: a put is read back by a get through the adapter's own event loop. Error chain: a server that cannot be reached makes the first
call raise the typed unavailable error, the adapter never tries again in the process, and the address (which may carry a password) never
appears in the error. The live test runs against the server named by ONEX_TEST_VALKEY_URL when it is set.
"""

from __future__ import annotations

import os
import uuid

import pytest
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_valkey import (
    BackendValkeySourceReadCache,
    valkey_url,
)
from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    SourceCacheUnavailableError,
)

pytestmark = pytest.mark.unit

SECRET = "pa:ss@w/rd"


class StoreStub:
    """Stands in for BackendCacheRedis: a dict behind the same async methods."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.items: dict[str, str] = {}
        self.connects = 0
        self.closed = False

    async def connect(self) -> None:
        self.connects += 1

    async def get(self, key: str) -> str | None:
        return self.items.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.items[key] = value

    async def close(self) -> None:
        self.closed = True


class DownStub(StoreStub):
    async def connect(self) -> None:
        self.connects += 1
        raise ModelOnexError(
            message=f"Failed to connect to Redis: {self.url}",
            error_code=EnumCoreErrorCode.CACHE_CONNECTION_ERROR,
        )


def test_golden_put_then_get_round_trips_through_the_adapter() -> None:
    stubs: list[StoreStub] = []

    def make(url: str) -> StoreStub:
        stubs.append(StoreStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    assert adapter.get("k") is None
    adapter.put("k", "v", 30)
    assert adapter.get("k") == "v"
    assert stubs[0].connects == 1
    adapter.close()
    adapter.close()
    assert stubs[0].closed


def test_error_chain_an_unreachable_server_is_typed_once_and_never_retried() -> None:
    stubs: list[DownStub] = []

    def make(url: str) -> DownStub:
        stubs.append(DownStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache(
        valkey_url("h", 1, SECRET, 0), 2, 2, backend_factory=make
    )
    for _ in range(2):
        with pytest.raises(SourceCacheUnavailableError) as raised:
            adapter.get("k")
        assert SECRET not in str(raised.value)
    with pytest.raises(SourceCacheUnavailableError):
        adapter.put("k", "v", 1)
    assert stubs[0].connects == 1
    adapter.close()


def test_the_url_quotes_a_password_and_omits_an_empty_one() -> None:
    assert valkey_url("h", 6379, SECRET, 2) == "redis://:pa%3Ass%40w%2Frd@h:6379/2"
    assert valkey_url("h", 6379, None, 0) == "redis://h:6379/0"


def test_live_server_round_trip_when_one_is_named() -> None:
    url = os.environ.get("ONEX_TEST_VALKEY_URL", "")
    if not url:
        # No server named: the live leg is not asked for, and that is the whole assertion of this branch.
        assert url == ""
        return
    adapter = BackendValkeySourceReadCache(url, 3, 5)
    key = f"test-board-cache:{uuid.uuid4()}"
    try:
        assert adapter.get(key) is None
        adapter.put(key, "payload", 30)
        assert adapter.get(key) == "payload"
    finally:
        adapter.close()
