# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane declares a second broker address; the bus opens through it when the first does not answer (OMN-20540).

The serve process reached the dev broker by one name only. When that name stopped answering, the consumer
could not subscribe and the ledger file froze. The lane declaration now carries ``broker_fallbacks`` (data),
each with the names the broker advertises and the address they resolve to while connected through it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Final

import pytest
import yaml
from click.testing import CliRunner
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import (
    InfraConnectionError,
    InfraTimeoutError,
    ModelTimeoutErrorContext,
)
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.lane_client_transport_binding import (
    bound_lane_client_transport,
)

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.handlers.bus_binding import broker_fallback
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.work_ledger_bus.cli import work_ledger_group

pytestmark = pytest.mark.unit

PRIMARY: Final = "primary.example.invalid:19092"
PRIMARY_HOST: Final = "primary.example.invalid"
SECOND: Final = "192.0.2.7:19092"
SECOND_IP: Final = "192.0.2.7"


class _FakeKafka:
    """A broker client that answers only at the addresses in ``answering``."""

    def __init__(self, bootstrap: str, answering: set[str], log: list[str]) -> None:
        self.bootstrap = bootstrap
        self._answering = answering
        self._log = log
        self.closed = False
        self.transport_address: str | None = None

    async def start(self) -> None:
        bound = bound_lane_client_transport()
        self.transport_address = bound.bootstrap_servers if bound else None
        self._log.append(self.bootstrap)
        if self.bootstrap not in self._answering:
            raise InfraConnectionError(f"Unable to bootstrap from {self.bootstrap}")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def declare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def write(*, fallbacks: object | None) -> Path:
        lane: dict[str, object] = {
            "broker": PRIMARY,
            "security_protocol": "PLAINTEXT",
        }
        if fallbacks is not None:
            lane["broker_fallbacks"] = fallbacks
        declaration = tmp_path / "config" / "lab" / "ci_bus_lanes.yaml"
        declaration.parent.mkdir(parents=True, exist_ok=True)
        declaration.write_text(
            yaml.safe_dump({"lanes": {"dev": lane}}), encoding="utf-8"
        )
        monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
        return tmp_path

    return write


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, object] = {"answering": set(), "attempts": [], "clients": []}

    def from_bootstrap(cls: type[EventBusKafka], bootstrap: str) -> _FakeKafka:
        client = _FakeKafka(bootstrap, state["answering"], state["attempts"])  # type: ignore[arg-type]
        state["clients"].append(client)  # type: ignore[attr-defined]
        return client

    monkeypatch.setattr(EventBusKafka, "from_bootstrap", classmethod(from_bootstrap))
    return state


_open = positional_opener(open_lab_run_bus)
DECLARED = [{"bootstrap": SECOND, "resolve": {PRIMARY_HOST: SECOND_IP}}]


def test_first_address_answers_so_the_second_is_never_tried(
    declare, broker, caplog: pytest.LogCaptureFixture
) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = {PRIMARY, SECOND}

    async def run() -> None:
        async with _open("kafka", "dev", None, root) as bus:
            assert bus.bootstrap == PRIMARY  # type: ignore[attr-defined]

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert broker["attempts"] == [PRIMARY]
    assert any(
        PRIMARY in r.getMessage() and "primary" in r.getMessage()
        for r in caplog.records
    )
    assert not any("switch" in r.getMessage().lower() for r in caplog.records)


def test_first_address_does_not_answer_so_the_second_carries_the_bus(
    declare, broker, caplog: pytest.LogCaptureFixture
) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = {SECOND}
    seen: dict[str, object] = {}

    async def run() -> None:
        loop = asyncio.get_running_loop()
        resolved: list[str] = []
        original = loop.getaddrinfo

        async def recording(host, port, *a, **k):  # type: ignore[no-untyped-def]
            resolved.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

        loop.getaddrinfo = recording  # type: ignore[method-assign]
        try:
            async with _open("kafka", "dev", None, root) as bus:
                seen["bootstrap"] = bus.bootstrap  # type: ignore[attr-defined]
                # the broker advertises the first name; while connected through the second
                # address that name is dialled at the declared address
                await loop.getaddrinfo(PRIMARY_HOST, 19092)
                await loop.getaddrinfo("unrelated.example.invalid", 19092)
                seen["during"] = list(resolved)
            # leaving the bus lifts the override: the loop's resolver is the test's own recorder again
            seen["after_override_lifted"] = loop.getaddrinfo is recording
        finally:
            loop.getaddrinfo = original  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert broker["attempts"] == [PRIMARY, SECOND]
    assert seen["bootstrap"] == SECOND
    assert seen["during"] == [SECOND_IP, "unrelated.example.invalid"]
    assert seen["after_override_lifted"] is True
    # the primary client was released before the second opened
    assert broker["clients"][0].closed is True  # type: ignore[index]
    # the transport the second client was built under is bound to the second address
    assert broker["clients"][1].transport_address == SECOND  # type: ignore[index]
    messages = [r.getMessage() for r in caplog.records]
    switch = [m for m in messages if "switch" in m.lower()]
    assert len(switch) == 1
    assert PRIMARY in switch[0]
    assert SECOND in switch[0]
    assert "count=1" in switch[0]
    assert any(
        r.levelno >= logging.WARNING and "switch" in r.getMessage().lower()
        for r in caplog.records
    ), "a switch is a warning, never a quiet info line"


def test_a_connect_that_times_out_is_a_connect_that_failed(
    declare, broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = {SECOND}

    monkeypatch.setattr(broker_fallback, "CONNECT_TIMEOUT_S", 0.2)

    async def hung_then_ok() -> None:
        async def start_primary_hangs(self: _FakeKafka) -> None:
            self._log.append(self.bootstrap)
            if self.bootstrap == PRIMARY:
                await asyncio.sleep(30)

        monkeypatch.setattr(_FakeKafka, "start", start_primary_hangs)
        async with _open("kafka", "dev", None, root) as bus:
            assert bus.bootstrap == SECOND  # type: ignore[attr-defined]

    asyncio.run(hung_then_ok())
    assert broker["attempts"] == [PRIMARY, SECOND]


def test_an_infra_timeout_is_a_failed_address_not_a_traceback(
    declare, broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = {SECOND}

    async def start(self: _FakeKafka) -> None:
        self._log.append(self.bootstrap)
        if self.bootstrap == PRIMARY:
            raise InfraTimeoutError(
                "Timeout connecting to Kafka after 30s",
                context=ModelTimeoutErrorContext(
                    transport_type=EnumInfraTransportType.KAFKA,
                    operation="start",
                    target_name="kafka.test",
                    timeout_seconds=30,
                ),
            )

    monkeypatch.setattr(_FakeKafka, "start", start)

    async def run() -> None:
        async with _open("kafka", "dev", None, root) as bus:
            assert bus.bootstrap == SECOND  # type: ignore[attr-defined]

    asyncio.run(run())


def test_both_addresses_down_fails_closed_naming_both(declare, broker) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = set()

    async def run() -> None:
        async with _open("kafka", "dev", None, root):
            raise AssertionError("no address answered, the bus must not open")

    with pytest.raises(LabRunBusError) as raised:
        asyncio.run(run())
    text = str(raised.value)
    assert PRIMARY in text
    assert SECOND in text
    assert "dev" in text
    assert broker["attempts"] == [PRIMARY, SECOND]
    assert all(c.closed for c in broker["clients"])  # type: ignore[attr-defined]


def test_serve_exits_69_naming_both_addresses_when_neither_answers(
    declare, broker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = set()
    monkeypatch.setenv("ONEX_WORK_LEDGER_WRITER_PRINCIPALS", "operator")
    monkeypatch.setenv("ONEX_WORK_LEDGER_OPERATOR_PRINCIPAL", "operator")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# Ledger\n", encoding="utf-8")
    result = CliRunner().invoke(
        work_ledger_group,
        [
            "serve",
            "--omnibase-path",
            str(root),
            "--bus",
            "kafka",
            "--bus-lane",
            "dev",
            "--host-name",
            "ledger-host",
            "--ledger",
            str(ledger),
            "--append-command",
            "true",
        ],
    )
    assert result.exit_code == 69
    assert PRIMARY in result.stderr
    assert SECOND in result.stderr


def test_a_lane_with_no_declared_fallback_behaves_as_before(declare, broker) -> None:
    root = declare(fallbacks=None)
    broker["answering"] = set()

    async def run() -> None:
        async with _open("kafka", "dev", None, root):
            raise AssertionError("must not open")

    with pytest.raises(LabRunBusError) as raised:
        asyncio.run(run())
    assert PRIMARY in str(raised.value)
    assert broker["attempts"] == [PRIMARY]


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-list",
        [{"resolve": {PRIMARY_HOST: SECOND_IP}}],
        [{"bootstrap": "", "resolve": {}}],
        [{"bootstrap": SECOND, "resolve": ["not", "a", "mapping"]}],
        [{"bootstrap": SECOND, "surprise": 1}],
        [{"bootstrap": PRIMARY}],
    ],
)
def test_a_malformed_declaration_is_refused_not_ignored(declare, broker, bad) -> None:
    root = declare(fallbacks=bad)
    broker["answering"] = {PRIMARY}

    async def run() -> None:
        async with _open("kafka", "dev", None, root):
            raise AssertionError("a malformed fallback declaration must refuse")

    with pytest.raises(LabRunBusError) as raised:
        asyncio.run(run())
    assert "broker_fallbacks" in str(raised.value)
    assert broker["attempts"] == []


def test_an_explicit_kafka_bootstrap_takes_no_fallback(declare, broker) -> None:
    root = declare(fallbacks=DECLARED)
    broker["answering"] = set()

    async def run() -> None:
        async with _open("kafka", None, PRIMARY, root):
            raise AssertionError("must not open")

    with pytest.raises(LabRunBusError):
        asyncio.run(run())
    assert broker["attempts"] == [PRIMARY]


def _edge(root: Path, known: object) -> None:
    (root / "config" / "lab" / "hook_edge_lane.yaml").write_text(
        yaml.safe_dump({"known_lanes": known}), encoding="utf-8"
    )


SOURCED = [{"source": "hook_edge_lane", "advertised_names": [PRIMARY_HOST]}]


def test_a_sourced_fallback_takes_the_address_the_edge_lane_file_declares(
    declare, broker
) -> None:
    root = declare(fallbacks=SOURCED)
    _edge(root, {"dev": {"bootstrap_servers": SECOND}})
    broker["answering"] = {SECOND}

    async def run() -> None:
        loop = asyncio.get_running_loop()
        dialled: list[str] = []
        original = loop.getaddrinfo

        async def recording(host, port, *a, **k):  # type: ignore[no-untyped-def]
            dialled.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

        loop.getaddrinfo = recording  # type: ignore[method-assign]
        try:
            async with _open("kafka", "dev", None, root) as bus:
                assert bus.bootstrap == SECOND  # type: ignore[attr-defined]
                await loop.getaddrinfo(PRIMARY_HOST, 19092)
        finally:
            loop.getaddrinfo = original  # type: ignore[method-assign]
        assert dialled == [SECOND_IP]

    asyncio.run(run())
    assert broker["attempts"] == [PRIMARY, SECOND]


@pytest.mark.parametrize(
    "known",
    [None, {"stability-test": {"bootstrap_servers": SECOND}}, {"dev": {}}],
)
def test_a_sourced_fallback_with_no_edge_address_is_refused(
    declare, broker, known
) -> None:
    root = declare(fallbacks=SOURCED)
    if known is not None:
        _edge(root, known)
    broker["answering"] = {PRIMARY}

    async def run() -> None:
        async with _open("kafka", "dev", None, root):
            raise AssertionError("an unreadable sourced fallback must refuse")

    with pytest.raises(LabRunBusError) as raised:
        asyncio.run(run())
    assert "broker_fallbacks" in str(raised.value)
    assert "hook_edge_lane" in str(raised.value)
    assert broker["attempts"] == []
