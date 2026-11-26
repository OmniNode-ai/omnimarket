# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The set command holds one lane bus at a time: the live read finishes before the write opens its bus (OMN-23563).

Run against a lane broker the command opened the write bus first and held it while the live-record read opened a
second connection, so two lane buses were open at once and the set could not reach the store. The read now closes its
bus before the write opens its own.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

import pytest

from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
from omnimarket.nodes.node_overlay_setting_set_effect import runtime_overlay_setting_set
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    ACTOR,
    BOOT_SET,
    KEY,
    OVERLAY_ID,
    live_record,
)

pytestmark = pytest.mark.unit


class _Probe:
    """Counts the buses open at once and records what each one carried."""

    def __init__(self, live: dict[str, Any]) -> None:
        self.live = live
        self.open_now = 0
        self.max_open = 0
        self.opened = 0
        self.published: list[str] = []


class _Bus:
    def __init__(self, probe: _Probe) -> None:
        self._probe = probe

    async def __aenter__(self) -> _Bus:
        self._probe.opened += 1
        self._probe.open_now += 1
        self._probe.max_open = max(self._probe.max_open, self._probe.open_now)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._probe.open_now -= 1

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Any],
        group_id: str,
        **_extra: Any,
    ) -> Callable[[], Any]:
        class _Message:
            value = json.dumps(self._probe.live).encode("utf-8")

        await on_message(_Message())

        async def stop() -> None:
            return None

        return stop

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        self._probe.published.append(topic)


def _run(monkeypatch: pytest.MonkeyPatch, probe: _Probe) -> None:
    monkeypatch.setattr(
        runtime_overlay_setting_set,
        "open_lane_bus",
        lambda **_kwargs: _Bus(probe),
    )
    request = ModelOverlaySettingSetRequest(
        overlay_id=OVERLAY_ID, key=KEY, value=BOOT_SET, actor=ACTOR
    )
    overlay = ModelOverlayStoreLoadOverlay(bus="kafka", bus_lane="dev", loaded_by="t")
    asyncio.run(runtime_overlay_setting_set._run(request, overlay, None, 0.01))


def _fresh_live() -> dict[str, Any]:
    return live_record(loaded_at=datetime.now(UTC))


def test_the_command_never_holds_two_lane_buses_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _Probe(_fresh_live())
    _run(monkeypatch, probe)
    assert probe.max_open == 1


def test_the_command_still_reads_then_writes_the_record_and_the_audit_row_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _Probe(_fresh_live())
    _run(monkeypatch, probe)
    assert len(probe.published) == 2
    assert probe.opened == 2
