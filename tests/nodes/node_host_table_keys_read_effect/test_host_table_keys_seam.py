# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam of the two host table key nodes (OMN-23983): the key layout the ingest writes is the one the reader reads.

One store, ingest then read: READ with the rows the host table node holds. The failing controls: a reader with another
key prefix, an empty store, a stale document, an empty row list and a malformed value all read UNKNOWN, never an empty list.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_table_compute.handlers.handler_host_table import (
    HandlerHostTable,
)
from omnimarket.nodes.node_host_table_compute.models import ModelHostTableRequest
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.handler_host_table_keys_ingest import (
    HandlerHostTableKeysIngest,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from omnimarket.nodes.node_host_table_keys_read_effect.handlers.handler_host_table_keys_read import (
    HandlerHostTableKeysRead,
)
from omnimarket.nodes.node_host_table_keys_read_effect.models.model_host_table_keys_read import (
    ModelHostTableKeysReadRequest,
)
from tests.nodes.node_host_table_compute.table_support import three_hosts
from tests.nodes.node_host_table_keys_ingest_effect.support import (
    NOW,
    PREFIX,
    overlay,
    record,
    registry_document,
)

pytestmark = pytest.mark.unit

REQUEST = ModelHostTableKeysReadRequest()


def ingested() -> StoreHostKeysInMemory:
    store = StoreHostKeysInMemory()
    HandlerHostTableKeysIngest(
        store=store, overlay=overlay(), clock=lambda: NOW
    ).handle(record(registry_document(*three_hosts())))
    return store


def reader(store: StoreHostKeysInMemory, *, prefix: str = PREFIX, later: int = 0):
    return HandlerHostTableKeysRead(
        store=store,
        overlay=overlay(key_prefix=prefix),
        clock=lambda: NOW + timedelta(seconds=later),
    )


def test_read_rows_equal_the_host_table_node_rows() -> None:
    reading = reader(ingested()).handle(REQUEST)
    assert reading.state == "READ"
    assert reading.document is not None
    expected = HandlerHostTable().handle(
        ModelHostTableRequest(
            table=ModelLabHostsTable.model_validate(registry_document(*three_hosts()))
        )
    )
    assert reading.document.table.rows == expected.rows
    assert len(reading.document.table.rows) == 3


@pytest.mark.parametrize(
    ("case", "prefix", "later", "reason"),
    [
        ("another prefix", "other:prefix", 0, "no host table keys"),
        ("stale", PREFIX, 601, "stale"),
    ],
)
def test_keys_that_cannot_be_read_read_unknown_never_an_empty_list(
    case: str, prefix: str, later: int, reason: str
) -> None:
    reading = reader(ingested(), prefix=prefix, later=later).handle(REQUEST)
    assert reading.state == "UNKNOWN", case
    assert reading.document is None
    assert reason in reading.reason


def test_an_empty_store_reads_unknown() -> None:
    reading = reader(StoreHostKeysInMemory()).handle(REQUEST)
    assert reading.state == "UNKNOWN"
    assert reading.document is None
    assert "no host table keys" in reading.reason


def test_a_stored_empty_row_list_and_a_malformed_value_read_unknown() -> None:
    store = StoreHostKeysInMemory()
    store.set(f"{PREFIX}:table", "not json")
    malformed = reader(store).handle(REQUEST)
    assert malformed.state == "UNKNOWN"
    assert "malformed" in malformed.reason
    base = ingested().get(f"{PREFIX}:table")
    assert base is not None
    import json

    doc = json.loads(base)
    doc["table"] = {"rows": [], "admitted": [], "excluded": []}
    store.set(f"{PREFIX}:table", json.dumps(doc))
    empty = reader(store).handle(REQUEST)
    assert empty.state == "UNKNOWN"
    assert empty.document is None


def test_a_store_that_does_not_answer_reads_unknown() -> None:
    class Down:
        def get(self, key: str) -> str | None:
            raise ConnectionError("down")

        def set(self, key: str, value: str) -> None:
            raise ConnectionError("down")

    reading = HandlerHostTableKeysRead(
        store=Down(), overlay=overlay(), clock=lambda: NOW
    ).handle(REQUEST)
    assert reading.state == "UNKNOWN"
    assert "ConnectionError" in reading.reason
