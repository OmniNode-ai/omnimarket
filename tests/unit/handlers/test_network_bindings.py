# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The network-binding authority serves every previous value and fails closed (OMN-21623)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.runtime.transport import InMemoryBroker, InMemoryTransport

from omnimarket.handlers import network_bindings
from omnimarket.handlers.container.container_inmemory import (
    bind_broker,
    bind_core_event_bus,
    bind_database_adapter,
    bind_infra_event_bus,
    bind_transport,
)
from omnimarket.handlers.network_bindings import (
    network_host,
    network_hosts,
    network_port,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.container.container_row_store import (
    bind_process_row_store,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


@pytest.mark.unit
def test_authority_serves_the_previous_values() -> None:
    assert network_hosts("loopback.hosts") == ("127.0.0.1", "localhost", "::1")
    assert network_host("loopback.ipv4") == "127.0.0.1"
    assert network_host("loopback.name") == "localhost"
    assert network_host("all_interfaces") == "0.0.0.0"
    assert network_host("emit_daemon.host") == "127.0.0.1"
    assert network_port("emit_daemon.port") == 9877
    assert network_port("projection_api.port") == 3002
    assert network_port("valkey.port") == 6379


@pytest.mark.unit
@pytest.mark.parametrize(
    ("document", "reader", "reference"),
    [
        ("loopback: {}\n", network_host, "loopback.ipv4"),
        ("loopback: {ipv4: ''}\n", network_host, "loopback.ipv4"),
        ("loopback: {hosts: []}\n", network_hosts, "loopback.hosts"),
        ("loopback: {hosts: [1]}\n", network_hosts, "loopback.hosts"),
        ("valkey: {port: 0}\n", network_port, "valkey.port"),
        ("valkey: {port: 70000}\n", network_port, "valkey.port"),
        ("valkey: {port: true}\n", network_port, "valkey.port"),
        ("valkey: {port: '6379'}\n", network_port, "valkey.port"),
    ],
)
def test_authority_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: str,
    reader: Callable[[str], object],
    reference: str,
) -> None:
    authority = tmp_path / "network_bindings.yaml"
    authority.write_text(document, encoding="utf-8")
    monkeypatch.setattr(network_bindings, "_AUTHORITY_PATH", authority)
    network_bindings._load_authority.cache_clear()
    try:
        with pytest.raises(KeyError):
            reader(reference)
    finally:
        network_bindings._load_authority.cache_clear()


@pytest.mark.unit
def test_authority_fails_closed_on_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(network_bindings, "_AUTHORITY_PATH", tmp_path / "absent.yaml")
    network_bindings._load_authority.cache_clear()
    try:
        with pytest.raises(FileNotFoundError):
            network_host("loopback.ipv4")
    finally:
        network_bindings._load_authority.cache_clear()


@pytest.mark.unit
def test_binding_sites_build_the_in_memory_implementations() -> None:
    assert isinstance(
        bind_core_event_bus(environment="test", group="g"), EventBusInmemory
    )
    assert type(bind_infra_event_bus(environment="test", group="g")).__name__ == (
        "EventBusInmemory"
    )
    broker = bind_broker()
    assert isinstance(broker, InMemoryBroker)
    assert isinstance(
        bind_transport(broker=broker, group="g", topics=["t"]), InMemoryTransport
    )
    assert isinstance(bind_process_row_store(), InMemoryPrHandoffRowStore)
    assert isinstance(bind_database_adapter(), InmemoryDatabaseAdapter)
