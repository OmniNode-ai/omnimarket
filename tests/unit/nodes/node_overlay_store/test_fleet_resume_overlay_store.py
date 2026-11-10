# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: the fleet record keeps up with a topic that outgrows one read window.

The floor row read "no reading for 41,854 s" on five hosts because the launching host's tick read the reading
topic from its earliest offset under a fresh consumer group for a fixed window, and its link carried the first
2,730 of 3,526 readings in that window: every host's newest reading stuck at offsets 2,724 to 2,728 while the
topic went on growing. These tests give the reader a bus that delivers only a few readings per window.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_read as read_module,
)
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_tick as tick_module,
)
from omnimarket.nodes.node_overlay_store_read_effect.runtime_overlay_store_tick import (
    main as tick_main,
)

HOSTS = ("h1", "h2", "h3", "h4", "h5", "h6")
ROUNDS = 5
PER_WINDOW = 7


def _reading(host: str, round_: int) -> dict[str, Any]:
    return {
        "host": host,
        "overlay_id": "lab_hosts",
        "state": "fresh",
        "source": "store",
        "read_at": f"2026-11-09T03:{round_:02d}:00.000000Z",
    }


class _Broker:
    """One topic log and the positions groups committed on it; a window delivers at most ``per_window``."""

    def __init__(self, per_window: int) -> None:
        self.per_window = per_window
        self.log = [
            json.dumps(_reading(host, round_)).encode()
            for round_ in range(ROUNDS)
            for host in HOSTS
        ]
        self.committed: dict[str, int] = {}
        self.groups: list[str] = []

    @contextlib.asynccontextmanager
    async def open(self, **_: object) -> AsyncIterator[SimpleNamespace]:
        async def subscribe(
            topic: str,
            *,
            on_message: Callable[[Any], Awaitable[None]],
            group_id: str,
            auto_offset_reset: str,
        ) -> Callable[[], Awaitable[None]]:
            assert auto_offset_reset == "earliest"
            self.groups.append(group_id)
            start = self.committed.get(group_id, 0)
            stop_at = min(start + self.per_window, len(self.log))
            for value in self.log[start:stop_at]:
                await on_message(SimpleNamespace(value=value))
            self.committed[group_id] = stop_at

            async def stop() -> None:
                return None

            return stop

        yield SimpleNamespace(subscribe=subscribe)


def _overlay(tmp_path: Path) -> dict[str, str]:
    node = tmp_path / "overlays" / "node_overlay_store_read_effect"
    node.mkdir(parents=True)
    (node / "overlay.yaml").write_text(
        "\n".join(
            [
                "backend: in_memory",
                f"snapshot_root: {tmp_path / 'snapshot'}",
                "read_window_s: 0.01",
                "host_name_env: HOST",
                "overlays:",
                "  lab_hosts: {stale_after_s: 60, on_stale: read}",
                "fleet_record_relpath: state/fleet.json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"), "HOST": "h0"}


def _tick(tmp_path: Path) -> dict[str, dict[str, Any]]:
    tick_main(["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "run")])
    record = json.loads((tmp_path / "state" / "fleet.json").read_text("utf-8"))
    readings: dict[str, dict[str, Any]] = record["readings"]
    return readings


def _newest_round(readings: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        key: reading["read_at"]
        for key, reading in readings.items()
        if key.split(":")[0] in HOSTS
    }


NEWEST = f"2026-11-09T03:{ROUNDS - 1:02d}:00.000000Z"


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Broker:
    for key, value in _overlay(tmp_path).items():
        monkeypatch.setenv(key, value)
    held = _Broker(PER_WINDOW)
    monkeypatch.setattr(read_module, "open_lane_bus", held.open)
    return held


def test_ticks_that_each_reach_part_of_the_topic_end_on_every_hosts_newest_reading(
    tmp_path: Path, broker: _Broker
) -> None:
    ticks = -(-len(broker.log) // PER_WINDOW)
    readings: dict[str, dict[str, Any]] = {}
    for _ in range(ticks):
        readings = _tick(tmp_path)
    assert _newest_round(readings) == {f"{host}:lab_hosts": NEWEST for host in HOSTS}
    assert broker.groups == [f"{read_module.NODE}.fleet.h0"] * ticks


def test_the_record_is_never_older_than_the_last_one_after_a_tick_that_reads_nothing_new(
    tmp_path: Path, broker: _Broker
) -> None:
    for _ in range(-(-len(broker.log) // PER_WINDOW)):
        _tick(tmp_path)
    before = _tick(tmp_path)
    after = _tick(tmp_path)
    assert _newest_round(after) == _newest_round(before)
    assert {key for key in after if key.split(":")[0] in HOSTS} == {
        f"{host}:lab_hosts" for host in HOSTS
    }


def test_a_group_of_its_own_per_host_is_what_keeps_the_position(
    tmp_path: Path, broker: _Broker
) -> None:
    _tick(tmp_path)
    assert list(broker.committed) == [f"{read_module.NODE}.fleet.h0"]
    assert broker.committed[f"{read_module.NODE}.fleet.h0"] == PER_WINDOW


def test_named_break_a_fresh_group_every_tick_leaves_the_record_stuck_on_the_first_window(
    tmp_path: Path, broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control: the reader as it was, a new group each tick, never gets past the first window."""
    real = read_module.fleet

    async def as_before(
        overlay: Any, env: Any, bootstrap: Any, group: str | None = None
    ) -> dict[str, Any]:
        return await real(overlay, env, bootstrap)

    monkeypatch.setattr(tick_module, "fleet", as_before)
    readings: dict[str, dict[str, Any]] = {}
    for _ in range(-(-len(broker.log) // PER_WINDOW)):
        readings = _tick(tmp_path)
    seen = _newest_round(readings)
    assert seen
    assert set(seen.values()) != {NEWEST}
    assert max(seen.values()) < NEWEST


def test_a_broker_that_cannot_be_reached_keeps_the_held_record_and_this_hosts_reading(
    tmp_path: Path, broker: _Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(-(-len(broker.log) // PER_WINDOW)):
        _tick(tmp_path)

    async def refused(*_: object) -> dict[str, object]:
        raise ConnectionError("broker down")

    monkeypatch.setattr(tick_module, "fleet", refused)
    readings = _tick(tmp_path)
    assert _newest_round(readings) == {f"{host}:lab_hosts": NEWEST for host in HOSTS}
    assert readings["h0:lab_hosts"]["state"] == "unreadable"


def test_a_held_record_that_cannot_be_parsed_is_replaced_by_what_the_bus_holds(
    tmp_path: Path, broker: _Broker
) -> None:
    held = tmp_path / "state" / "fleet.json"
    held.parent.mkdir()
    held.write_text("{not json", encoding="utf-8")
    readings = _tick(tmp_path)
    assert readings["h0:lab_hosts"]["state"] == "unreadable"
    assert set(_newest_round(readings)) == {f"{host}:lab_hosts" for host in HOSTS}
