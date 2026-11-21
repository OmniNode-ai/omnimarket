# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26027: a bus client connects inside a declared deadline and through the lane's second address."""

from __future__ import annotations

import asyncio
import inspect
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError, open_lab_run_bus
from omnimarket.handlers.bus_connect.errors import (
    BusConnectDeadlineError,
    BusConnectError,
)
from omnimarket.handlers.bus_connect.open_kafka import (
    advertised_names_dialled_at,
    open_kafka_bus,
)
from omnimarket.handlers.bus_connect.policy import (
    load_broker_fallbacks,
    load_connect_policy,
)
from omnimarket.models.bus_connect import ModelBrokerFallback, ModelBusConnectPolicy

# RFC 5737 TEST-NET-1: reserved for documentation, never a real host.
UNROUTABLE = "192.0.2.1:19092"
SHORT = ModelBusConnectPolicy(connect_deadline_seconds=1.0, abandon_grace_seconds=0.5)


class _Bus:
    def __init__(self, address: str, *, fail: bool) -> None:
        self.address, self.fail, self.closed = address, fail, False

    async def start(self) -> None:
        if self.fail:
            raise InfraConnectionError(f"cannot resolve {self.address}")

    async def close(self) -> None:
        self.closed = True


def test_bus_connect_deadline_returns_typed_error_inside_deadline() -> None:
    async def attempt() -> float:
        started = time.monotonic()
        with pytest.raises(BusConnectDeadlineError) as raised:
            async with open_kafka_bus(
                bootstrap=UNROUTABLE,
                transport=None,
                fallbacks=(),
                policy=SHORT,
                make_bus=EventBusKafka.from_bootstrap,
            ):
                pytest.fail("a blackholed address must not start")
        assert raised.value.attempts[0].address == UNROUTABLE
        assert isinstance(raised.value, LabRunBusError)
        return time.monotonic() - started

    elapsed = asyncio.run(attempt())
    assert elapsed < SHORT.connect_deadline_seconds + 1


def _open_arguments(**given: str) -> dict[str, Any]:
    """Every parameter of open_lab_run_bus, None unless given."""
    return {
        name: given.get(name) for name in inspect.signature(open_lab_run_bus).parameters
    }


def test_bus_connect_deadline_open_lab_run_bus_uses_declared_deadline() -> None:
    declared = load_connect_policy().connect_deadline_seconds

    async def attempt() -> float:
        started = time.monotonic()
        with pytest.raises(BusConnectError):
            async with open_lab_run_bus(
                **_open_arguments(bus="kafka", kafka_bootstrap=UNROUTABLE)
            ):
                pytest.fail("a blackholed address must not start")
        return time.monotonic() - started

    assert asyncio.run(attempt()) < declared + 1


def test_bus_connect_deadline_is_declared_not_coded(tmp_path: Path) -> None:
    assert load_connect_policy().connect_deadline_seconds > 0
    declaration = tmp_path / "ci_bus_lanes.yaml"
    declaration.write_text("lanes:\n  dev:\n    connect_deadline_seconds: 3.5\n")
    assert load_connect_policy(declaration, "dev").connect_deadline_seconds == 3.5


def _declaration(tmp_path: Path) -> Path:
    declaration = tmp_path / "ci_bus_lanes.yaml"
    declaration.write_text(
        "lanes:\n  dev:\n    broker: nonexistent.invalid:19092\n    broker_fallbacks:\n"
        "      - source: hook_edge_lane\n        advertised_names: [nonexistent.invalid]\n"
    )
    (tmp_path / "hook_edge_lane.yaml").write_text(
        'known_lanes:\n  dev:\n    bootstrap_servers: "198.51.100.10:19092"\n'
    )
    return declaration


def test_lan_bootstrap_second_address_is_read_from_declaration(tmp_path: Path) -> None:
    fallbacks = load_broker_fallbacks(_declaration(tmp_path), "dev")
    assert fallbacks == (
        ModelBrokerFallback(
            address="198.51.100.10:19092", advertised_names=("nonexistent.invalid",)
        ),
    )
    assert load_broker_fallbacks(tmp_path / "none.yaml", None) == ()


def test_lan_bootstrap_second_address_connects_when_tailnet_name_unresolvable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fallbacks = load_broker_fallbacks(_declaration(tmp_path), "dev")
    made: list[_Bus] = []

    def make(address: str) -> _Bus:
        made.append(_Bus(address, fail=address.startswith("nonexistent.invalid")))
        return made[-1]

    async def attempt() -> object:
        async with open_kafka_bus(
            bootstrap="nonexistent.invalid:19092",
            transport=None,
            fallbacks=fallbacks,
            policy=SHORT,
            make_bus=make,
        ) as bus:
            return bus

    bus = asyncio.run(attempt())
    assert [b.address for b in made] == [
        "nonexistent.invalid:19092",
        "198.51.100.10:19092",
    ]
    assert bus is made[1]
    assert made[0].closed
    assert made[1].closed
    assert "198.51.100.10:19092" in capsys.readouterr().err


def test_lan_bootstrap_second_address_fails_typed_when_both_fail() -> None:
    async def attempt() -> BusConnectError:
        with pytest.raises(BusConnectError) as raised:
            async with open_kafka_bus(
                bootstrap="nonexistent.invalid:19092",
                transport=None,
                fallbacks=(ModelBrokerFallback(address="other.invalid:19092"),),
                policy=SHORT,
                make_bus=lambda a: _Bus(a, fail=True),
            ):
                pytest.fail("no address starts")
        return raised.value

    error = asyncio.run(attempt())
    assert [a.address for a in error.attempts] == [
        "nonexistent.invalid:19092",
        "other.invalid:19092",
    ]
    assert not isinstance(error, BusConnectDeadlineError)


def test_lan_bootstrap_second_address_dials_advertised_name_at_its_host() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    async def attempt() -> str:
        loop = asyncio.get_running_loop()
        with advertised_names_dialled_at(
            ("tailnet-name.invalid",), f"127.0.0.1:{port}"
        ):
            infos = await loop.getaddrinfo("tailnet-name.invalid", port)
        # Positive control above; the shim is gone once the scope ends.
        with pytest.raises(socket.gaierror):
            await loop.getaddrinfo("tailnet-name.invalid", port)
        return str(infos[0][4][0])

    try:
        assert asyncio.run(attempt()) == "127.0.0.1"
    finally:
        listener.close()


def test_lan_bootstrap_second_address_comes_from_policy_when_lane_declares_none(
    tmp_path: Path,
) -> None:
    declaration = tmp_path / "ci_bus_lanes.yaml"
    declaration.write_text("lanes:\n  dev:\n    broker: nonexistent.invalid:19092\n")
    (tmp_path / "hook_edge_lane.yaml").write_text(
        'known_lanes:\n  dev:\n    bootstrap_servers: "198.51.100.10:19092"\n'
    )
    policy = load_connect_policy(declaration, "dev")
    assert policy.default_broker_fallbacks  # the packaged policy declares one
    fallbacks = load_broker_fallbacks(
        declaration, "dev", policy.default_broker_fallbacks
    )
    assert [f.address for f in fallbacks] == ["198.51.100.10:19092"]
    assert fallbacks[0].advertised_names_from_bootstrap
    # a lane the hook edge file does not know gets no second address, and no error
    assert (
        load_broker_fallbacks(
            declaration, "other-lane", policy.default_broker_fallbacks
        )
        == ()
    )


def test_lan_bootstrap_second_address_skips_a_fallback_equal_to_the_first() -> None:
    made: list[_Bus] = []

    def make(address: str) -> _Bus:
        made.append(_Bus(address, fail=True))
        return made[-1]

    async def attempt() -> None:
        with pytest.raises(BusConnectError):
            async with open_kafka_bus(
                bootstrap="198.51.100.10:19092",
                transport=None,
                fallbacks=(ModelBrokerFallback(address="198.51.100.10:19092"),),
                policy=SHORT,
                make_bus=make,
            ):
                pass

    asyncio.run(attempt())
    assert [b.address for b in made] == ["198.51.100.10:19092"]
