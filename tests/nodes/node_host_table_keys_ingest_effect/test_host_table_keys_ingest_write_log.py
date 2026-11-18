# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ingest handler leaves one INFO line when it writes a record and none when it ignores one."""

from __future__ import annotations

import logging

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

LOGGER = (
    "omnimarket.nodes.node_host_table_keys_ingest_effect.handlers."
    "handler_host_table_keys_ingest"
)


def _handler(store: StoreHostKeysInMemory) -> HandlerHostTableKeysIngest:
    return HandlerHostTableKeysIngest(store=store, overlay=overlay(), clock=lambda: NOW)


def _ingest_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def test_a_write_logs_one_info_line_with_key_digest_hosts_and_admitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    incoming = record(registry_document(*three_hosts()))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _handler(StoreHostKeysInMemory()).handle(incoming)
    lines = _ingest_lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    message = lines[0].getMessage()
    assert message.startswith("node_host_table_keys_ingest_effect wrote key=")
    assert f"key={PREFIX}:table" in message
    assert f"digest={incoming.digest}" in message
    assert "hosts=3" in message
    assert "admitted=2" in message


def test_an_ignored_record_logs_no_line(caplog: pytest.LogCaptureFixture) -> None:
    store = StoreHostKeysInMemory()
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _handler(store).handle(
            record(registry_document(*three_hosts()), overlay_id="other_overlay")
        )
    assert _ingest_lines(caplog) == []
    assert store.get(f"{PREFIX}:table") is None


def test_a_refused_record_logs_no_write_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    silent = registry_document(row("silent-row", available=False))
    with (
        caplog.at_level(logging.DEBUG, logger=LOGGER),
        pytest.raises(HostTableKeysRefusedError),
    ):
        _handler(StoreHostKeysInMemory()).handle(record(silent))
    assert _ingest_lines(caplog) == []
