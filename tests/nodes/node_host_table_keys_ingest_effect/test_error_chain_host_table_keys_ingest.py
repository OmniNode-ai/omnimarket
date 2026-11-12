# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain of the host table keys ingest node (OMN-23983): a refused record writes nothing."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.handler_host_table_keys_ingest import (
    HandlerHostTableKeysIngest,
    HostTableKeysRefusedError,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from tests.nodes.node_host_table_compute.table_support import row, three_hosts
from tests.nodes.node_host_table_keys_ingest_effect.support import (
    NOW,
    PREFIX,
    overlay,
    record,
    registry_document,
)

pytestmark = pytest.mark.unit


def handler(store: object) -> HandlerHostTableKeysIngest:
    return HandlerHostTableKeysIngest(
        store=store,  # type: ignore[arg-type]
        overlay=overlay(),
        clock=lambda: NOW,
    )


def test_a_host_excluded_without_a_reason_writes_nothing_and_the_stored_table_stays() -> (
    None
):
    store = StoreHostKeysInMemory()
    good = handler(store)
    good.handle(record(registry_document(*three_hosts())))
    before = store.get(f"{PREFIX}:table")
    silent = registry_document(row("silent-row", available=False))
    with pytest.raises(HostTableKeysRefusedError, match="silent-row"):
        good.handle(record(silent))
    assert store.get(f"{PREFIX}:table") == before


def test_another_overlay_is_ignored_and_writes_nothing() -> None:
    store = StoreHostKeysInMemory()
    result = handler(store).handle(
        record(registry_document(*three_hosts()), overlay_id="other_overlay")
    )
    assert result.outcome == "ignored"
    assert store.get(f"{PREFIX}:table") is None


def test_a_document_the_table_model_refuses_names_the_field() -> None:
    store = StoreHostKeysInMemory()
    bad = registry_document({"name": "no-target-row"})
    with pytest.raises(HostTableKeysRefusedError, match="target"):
        handler(store).handle(record(bad))
    assert store.get(f"{PREFIX}:table") is None


def test_a_store_that_fails_propagates_the_error() -> None:
    class Broken:
        def get(self, key: str) -> str | None:
            return None

        def set(self, key: str, value: str) -> None:
            raise ConnectionError("store down")

    with pytest.raises(ConnectionError):
        handler(Broken()).handle(record(registry_document(*three_hosts())))
