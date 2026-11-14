# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An unexpected error from the Valkey client is treated as "backend unavailable" (OMN-24294).

Golden: a put is read back by a get through the adapter's own event loop. Error chain: a client that fails with anything but a timeout (a
stream parser that cannot split its read buffer, a typed cache error after connecting) makes the first call raise the typed unavailable
error, the adapter never tries again in the process, and neither the address (which may carry a password) nor the underlying error's text
appears in the message; through the reader the step still gets the source's answer.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.models.model_source_read_cache import EnumSourceCacheOutcome
from omnimarket.nodes.node_source_read_cache_effect.handlers.backend_valkey import (
    BackendValkeySourceReadCache,
    valkey_url,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.protocols.protocol_source_read_cache_backend import (
    SourceCacheUnavailableError,
)
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    key,
)

pytestmark = pytest.mark.unit

SECRET = "pa:ss@w/rd"
SEPARATOR_ERROR = "Separator is not found, and chunk exceed the limit"


class StoreStub:
    """Stands in for BackendCacheRedis: a dict behind the same async methods."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.items: dict[str, str] = {}
        self.connects = 0
        self.gets = 0
        self.sets = 0
        self.closed = False

    async def connect(self) -> None:
        self.connects += 1

    async def get(self, key: str) -> str | None:
        self.gets += 1
        return self.items.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.sets += 1
        self.items[key] = value

    async def close(self) -> None:
        self.closed = True


class GetRaisesStub(StoreStub):
    """A store whose stream parser cannot split its read buffer on a get."""

    async def get(self, key: str) -> str | None:
        self.gets += 1
        raise ValueError(SEPARATOR_ERROR)


class SetRaisesStub(StoreStub):
    """A store whose stream parser cannot split its read buffer on a set."""

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.sets += 1
        raise ValueError(SEPARATOR_ERROR)


class GetRaisesOnexStub(StoreStub):
    """A store whose client reports a typed cache error on a get after connecting."""

    async def get(self, key: str) -> str | None:
        self.gets += 1
        raise ModelOnexError(
            message=f"cache read failed: {self.url}",
            error_code=EnumCoreErrorCode.CACHE_CONNECTION_ERROR,
        )


class Source:
    """A source that counts how often it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def fetch(self) -> list[int]:
        self.reads += 1
        return [1, 2, 3]


def test_golden_a_normal_store_still_round_trips_put_then_get() -> None:
    stubs: list[StoreStub] = []

    def make(url: str) -> StoreStub:
        stubs.append(StoreStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    adapter.put("k", "v", 30)
    assert adapter.get("k") == "v"
    assert stubs[0].connects == 1
    adapter.close()
    assert stubs[0].closed


def test_error_chain_a_value_error_on_a_get_is_typed_once_and_never_retried() -> None:
    stubs: list[GetRaisesStub] = []

    def make(url: str) -> GetRaisesStub:
        stubs.append(GetRaisesStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    for _ in range(2):
        with pytest.raises(SourceCacheUnavailableError):
            adapter.get("k")
    assert stubs[0].gets == 1
    adapter.close()


def test_error_chain_a_value_error_on_a_set_is_typed_once_and_never_retried() -> None:
    stubs: list[SetRaisesStub] = []

    def make(url: str) -> SetRaisesStub:
        stubs.append(SetRaisesStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    for _ in range(2):
        with pytest.raises(SourceCacheUnavailableError):
            adapter.put("k", "v", 1)
    assert stubs[0].sets == 1
    adapter.close()


def test_error_chain_a_typed_cache_error_after_connecting_is_typed_once_and_never_retried() -> (
    None
):
    stubs: list[GetRaisesOnexStub] = []

    def make(url: str) -> GetRaisesOnexStub:
        stubs.append(GetRaisesOnexStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    for _ in range(2):
        with pytest.raises(SourceCacheUnavailableError):
            adapter.get("k")
    assert (stubs[0].connects, stubs[0].gets) == (1, 1)
    adapter.close()


def test_the_error_message_never_carries_the_address_or_the_underlying_error() -> None:
    stubs: list[GetRaisesStub] = []

    def make(url: str) -> GetRaisesStub:
        stubs.append(GetRaisesStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache(
        valkey_url("h", 1, SECRET, 0), 2, 2, backend_factory=make
    )
    with pytest.raises(SourceCacheUnavailableError) as raised:
        adapter.get("k")
    message = str(raised.value)
    assert SECRET not in message
    assert SEPARATOR_ERROR not in message
    with pytest.raises(SourceCacheUnavailableError) as put_raised:
        adapter.put("k", "v", 1)
    message = str(put_raised.value)
    assert SECRET not in message
    assert SEPARATOR_ERROR not in message
    adapter.close()


def test_error_chain_through_the_reader_the_source_still_answers() -> None:
    stubs: list[GetRaisesStub] = []

    def make(url: str) -> GetRaisesStub:
        stubs.append(GetRaisesStub(url))
        return stubs[-1]

    adapter = BackendValkeySourceReadCache("redis://x", 2, 2, backend_factory=make)
    reader = CachedSourceReader(HandlerSourceReadCache(adapter, PREFIX, TTL_BACKSTOP))
    source = Source()
    value, outcome = reader.read(
        key(), source.fetch, json.dumps, lambda text: json.loads(text)
    )
    assert value == [1, 2, 3]
    assert outcome == EnumSourceCacheOutcome.UNAVAILABLE
    assert (reader.tally.unavailable, reader.tally.source_reads) == (1, 1)
    adapter.close()
