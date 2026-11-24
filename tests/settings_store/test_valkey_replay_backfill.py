# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (Task 20): the Valkey store takes every connection value from the wiring and replays to the backfilled revisions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_backend_registry import (
    StoreWiringRefusedError,
)
from omnimarket.handlers.overlay_store.store_valkey import (
    ValkeyOverlayStore,
    ValkeyStoreReading,
)
from omnimarket.handlers.overlay_store.store_wiring import (
    KIND_INPUT,
    build_overlay_store,
)

T0 = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class _FakeValkey:
    """The two calls the store makes of an asyncio redis client."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.writes = 0

    async def get(self, name: str) -> str | None:
        return self.data.get(name)

    async def set(self, name: str, value: str) -> object:
        self.writes += 1
        self.data[name] = value
        return True


def _record(
    overlay_id: str,
    revision: int | None,
    *,
    heartbeat: datetime | None = None,
    value: object = 0,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "overlay_id": overlay_id,
        "digest": "d" * 64,
        "document": {"value": value},
        "source_ref": "ref",
        "loaded_by": "test",
        "loaded_at": T0.isoformat(),
    }
    if revision is not None:
        record["revision"] = revision
    if heartbeat is not None:
        record["heartbeat_at"] = heartbeat.isoformat()
    return record


# A topic that began before revisions existed: two records carry neither revision nor heartbeat.
TOPIC = [
    _record("alpha", None, value="pre-1"),
    _record("beta", None, value="pre-2"),
    _record("alpha", 1, heartbeat=T0, value="a1"),
    _record("beta", 1, heartbeat=T0, value="b1"),
    _record("alpha", 2, heartbeat=T0 + timedelta(seconds=5), value="a2"),
]


async def _replay(store: ValkeyOverlayStore, records: list[dict[str, Any]]) -> None:
    for record in records:
        await store.write_version(record)


async def test_valkey_replay_backfill_into_an_empty_prefix_ends_at_the_backfilled_revisions() -> (
    None
):
    store = ValkeyOverlayStore(_FakeValkey(), key_prefix="p")
    await _replay(store, TOPIC)
    alpha = await store.read_current("alpha")
    beta = await store.read_current("beta")
    assert alpha is not None
    assert alpha["revision"] == 2
    assert beta is not None
    assert beta["revision"] == 1
    assert alpha["document"] == {"value": "a2"}


async def test_valkey_replay_backfill_counts_the_skipped_pre_revision_records() -> None:
    store = ValkeyOverlayStore(_FakeValkey(), key_prefix="p")
    await _replay(store, TOPIC)
    assert store.reading() == ValkeyStoreReading(applied=3, skipped_pre_revision=2)


async def test_valkey_replay_backfill_a_second_replay_changes_nothing() -> None:
    client = _FakeValkey()
    store = ValkeyOverlayStore(client, key_prefix="p")
    await _replay(store, TOPIC)
    before = dict(client.data)
    await _replay(store, TOPIC)
    assert client.data == before


async def test_valkey_replay_backfill_an_older_revision_never_moves_a_value_back() -> (
    None
):
    store = ValkeyOverlayStore(_FakeValkey(), key_prefix="p")
    await store.write_version(_record("alpha", 3, heartbeat=T0, value="new"))
    assert (
        await store.write_version(_record("alpha", 2, heartbeat=T0, value="old"))
        is False
    )
    current = await store.read_current("alpha")
    assert current is not None
    assert current["document"] == {"value": "new"}


async def test_valkey_replay_backfill_same_revision_later_heartbeat_is_applied() -> (
    None
):
    store = ValkeyOverlayStore(_FakeValkey(), key_prefix="p")
    await store.write_version(_record("alpha", 1, heartbeat=T0))
    later = T0 + timedelta(seconds=30)
    assert await store.write_version(_record("alpha", 1, heartbeat=later)) is True
    current = await store.read_current("alpha")
    assert current is not None
    assert current["heartbeat_at"] == later.isoformat()
    assert await store.write_version(_record("alpha", 1, heartbeat=T0)) is False


async def test_valkey_replay_backfill_keys_live_under_the_prefix_the_wiring_gave() -> (
    None
):
    client = _FakeValkey()
    store = ValkeyOverlayStore(client, key_prefix="from-the-wiring")
    await store.write_version(_record("alpha", 1, heartbeat=T0))
    assert list(client.data) == ["from-the-wiring:alpha"]
    assert json.loads(client.data["from-the-wiring:alpha"])["revision"] == 1


async def test_valkey_read_of_an_overlay_the_store_lacks_is_none() -> None:
    store = ValkeyOverlayStore(_FakeValkey(), key_prefix="p")
    assert await store.read_current("nothing") is None


async def test_valkey_an_unreachable_client_raises_and_is_not_none() -> None:
    class _Down(_FakeValkey):
        async def get(self, name: str) -> str | None:
            raise ConnectionError("closed")

    store = ValkeyOverlayStore(_Down(), key_prefix="p")
    with pytest.raises(ConnectionError):
        await store.read_current("alpha")


_VALKEY_INPUTS: Mapping[str, str] = {
    KIND_INPUT: "store_valkey",
    "ONEX_SETTINGS_STORE_ADDRESS": "from-bootstrap",
    "ONEX_SETTINGS_STORE_PORT": "1",
    "ONEX_SETTINGS_STORE_DATABASE": "0",
    "ONEX_SETTINGS_STORE_KEY_PREFIX": "from-bootstrap-prefix",
    "ONEX_SETTINGS_STORE_LOGIN": "reader-login",
}


@pytest.mark.parametrize("missing", sorted(set(_VALKEY_INPUTS) - {KIND_INPUT}))
def test_valkey_replay_backfill_wiring_refuses_a_missing_connection_value_by_name(
    missing: str,
) -> None:
    inputs = {k: v for k, v in _VALKEY_INPUTS.items() if k != missing}
    with pytest.raises(StoreWiringRefusedError, match=missing):
        build_overlay_store(
            inputs, fetch_secrets=lambda names: dict.fromkeys(names, "x")
        )


def test_valkey_replay_backfill_wiring_fetches_only_the_password_secret() -> None:
    asked: list[tuple[str, ...]] = []

    def fetch(names: tuple[str, ...]) -> Mapping[str, str]:
        asked.append(names)
        return dict.fromkeys(names, "x")

    store = build_overlay_store(dict(_VALKEY_INPUTS), fetch_secrets=fetch)
    assert isinstance(store, ValkeyOverlayStore)
    assert asked == [("ONEX_SETTINGS_STORE_PASSWORD",)]
