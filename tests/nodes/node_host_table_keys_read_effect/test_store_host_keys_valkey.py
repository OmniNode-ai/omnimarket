# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Valkey store of the host table keys (OMN-23983): built from the variables the overlay names, round-trips a value."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers import (
    store_host_keys,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    HostTableKeysStoreUnconfiguredError,
    StoreHostKeysValkey,
)
from tests.nodes.node_host_table_keys_ingest_effect.support import overlay

pytestmark = pytest.mark.unit

ENV = {
    "SYNTHETIC_KV_HOST": "kv.synthetic.invalid",
    "SYNTHETIC_KV_PORT": "6380",
    "SYNTHETIC_KV_SECRET": "synthetic-value",
}


class FakeClient:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


def test_a_value_round_trips_through_the_client() -> None:
    store = StoreHostKeysValkey(FakeClient())  # type: ignore[arg-type]
    store.set("k", "v")
    assert store.get("k") == "v"
    assert store.get("absent") is None


def test_the_client_is_built_from_the_variables_the_overlay_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, Any] = {}

    def fake_redis(**kwargs: Any) -> FakeClient:
        built.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(store_host_keys.redis, "Redis", fake_redis)
    StoreHostKeysValkey.from_environment(overlay(), ENV)
    assert built["host"] == "kv.synthetic.invalid"
    assert built["port"] == 6380
    assert built["socket_timeout"] == 3
    assert built["socket_connect_timeout"] == 3
    assert built["decode_responses"] is True


@pytest.mark.parametrize("missing", sorted(ENV))
def test_a_missing_variable_is_named(missing: str) -> None:
    environ = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(HostTableKeysStoreUnconfiguredError, match=missing):
        StoreHostKeysValkey.from_environment(overlay(), environ)
