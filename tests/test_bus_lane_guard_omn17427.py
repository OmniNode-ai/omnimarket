# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: a test never reaches a bus lane other than the declared one.

An operator machine exported KAFKA_BOOTSTRAP_SERVERS naming another team's lane,
and an integration run published test events and created topics there. These
tests plant that exact condition and prove the broker still comes from the
declared lane and that a broker of any other lane is refused before a publish.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests import conftest as suite_conftest
from tests.bus_lane_guard import (
    TEST_BUS_LANE_PATH,
    BusLaneRefusalError,
    assert_broker_in_declared_lane,
    declared_test_lane,
    resolve_test_bus_broker,
)

pytestmark = pytest.mark.unit

_OVERLAY = Path(__file__).resolve().parents[1] / "config" / "ci_bus_lanes.yaml"


def _overlay_lanes() -> dict[str, dict[str, object]]:
    loaded = yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))["lanes"]
    return {k: v for k, v in loaded.items() if isinstance(v, dict)}


def _lane_broker(lane: str) -> str:
    return str(_overlay_lanes()[lane]["broker"])


def _other_concrete_lanes() -> list[str]:
    declared = declared_test_lane()
    return [
        name
        for name, entry in _overlay_lanes().items()
        if name != declared
        and str(entry.get("broker", "")) not in ("", "inmemory", "from-secret")
    ]


def _on_another_host(broker: str) -> str:
    """The same port under a host name the overlay does not spell."""
    return "lan-alias.invalid:" + broker.rsplit(":", 1)[-1]


def test_declared_lane_has_a_concrete_broker() -> None:
    assert _lane_broker(declared_test_lane())
    assert TEST_BUS_LANE_PATH.is_file()
    assert _other_concrete_lanes()


@pytest.mark.parametrize("lane_index", [0, 1, 2])
def test_ambient_env_of_another_lane_does_not_choose_the_broker(
    monkeypatch: pytest.MonkeyPatch, lane_index: int
) -> None:
    other = _other_concrete_lanes()[lane_index % len(_other_concrete_lanes())]
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", _lane_broker(other))
    assert resolve_test_bus_broker() == _lane_broker(declared_test_lane())


def test_conftest_broker_is_the_declared_lane_broker() -> None:
    assert _lane_broker(declared_test_lane()) == suite_conftest._KAFKA_BOOTSTRAP


def test_every_other_lane_broker_is_refused() -> None:
    for lane in _other_concrete_lanes():
        with pytest.raises(BusLaneRefusalError, match="Nothing was published"):
            assert_broker_in_declared_lane(_lane_broker(lane))


def test_another_lane_under_an_aliased_host_is_refused() -> None:
    # Same port, a host name the overlay does not spell: port identity catches it.
    for lane in _other_concrete_lanes():
        with pytest.raises(BusLaneRefusalError, match=f"lane {lane!r}"):
            assert_broker_in_declared_lane(_on_another_host(_lane_broker(lane)))


def test_undeclared_broker_is_refused() -> None:
    with pytest.raises(BusLaneRefusalError, match="no declared lane"):
        assert_broker_in_declared_lane("localhost:9")


def test_declared_lane_broker_is_accepted() -> None:
    broker = _lane_broker(declared_test_lane())
    assert assert_broker_in_declared_lane(broker) == broker


async def test_kafka_probe_refuses_before_constructing_a_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With another lane's broker forced in, the fixture refuses and never connects."""
    import aiokafka

    constructed: list[object] = []

    class _Spy:
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append((args, kwargs))

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _Spy)
    monkeypatch.setattr(
        suite_conftest, "_KAFKA_BOOTSTRAP", _lane_broker(_other_concrete_lanes()[0])
    )

    class _Node:
        @staticmethod
        def get_closest_marker(_name: str) -> object:
            return object()

    class _Request:
        node = _Node()

    fixture = suite_conftest.integration_kafka_bootstrap
    inner = getattr(fixture, "__wrapped__", None) or fixture
    with pytest.raises(BusLaneRefusalError):
        await inner(_Request())
    assert constructed == []
