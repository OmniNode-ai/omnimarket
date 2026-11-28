# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot turns a live drain mark into a lane admission refusal (OMN-24489).

A host that advertises a live ``draining-until:`` pseudo-tool is refused with a reason naming the
expiry; a mark past its expiry gives no refusal, and an advertisement with no such tool gives none.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    build_pool_snapshot,
    default_overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models import (
    ModelHostAccounts,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    advertisement,
    host_row,
)

pytestmark = pytest.mark.unit


def _accounts(host: str) -> ModelHostAccounts:
    return ModelHostAccounts(
        host=host, usable=("acct-a",), limited=(), unread_reason=None
    )


def _refusal(ads: tuple, read_at: datetime) -> tuple[str | None, ...]:
    snapshot, _ = build_pool_snapshot(
        rows=(host_row("h1"),),
        ads=ads,
        lanes={},
        accounts=(_accounts("h1"),),
        overlay=default_overlay(),
        read_at=read_at,
    )
    return tuple(r.lane_admission_refusal for r in snapshot.readings)


def test_a_live_drain_mark_gives_a_draining_refusal() -> None:
    # Golden: a mark after the read time is refused, naming the expiry.
    (refusal,) = _refusal(
        (advertisement("h1", tools=("draining-until:2026-11-11T04:00:00Z",)),), T0
    )
    assert refusal is not None
    assert refusal.startswith("draining: until")
    assert "2026-11-11T04:00:00Z" in refusal


def test_a_mark_past_its_expiry_gives_no_refusal() -> None:
    # Control: a mark before the read time is expired and gives no refusal.
    later = T0 + timedelta(hours=2)
    (refusal,) = _refusal(
        (
            advertisement(
                "h1",
                advertised_at=later,
                tools=("draining-until:2026-11-11T04:00:00Z",),
            ),
        ),
        later,
    )
    assert refusal is None


def test_an_advertisement_with_no_drain_tool_gives_no_refusal() -> None:
    # Control: no ``draining-until:`` tool gives no refusal.
    (refusal,) = _refusal((advertisement("h1"),), T0)
    assert refusal is None
