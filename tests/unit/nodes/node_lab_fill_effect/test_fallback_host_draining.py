# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill fallback host skips a draining host, read live like the limited markers (OMN-24489).

The drain state is read at every fallback through the injected callable, so a host the drain verb
is draining is never pinned. A drain read that fails offers no host, exactly as an unreadable
limited marker does today: a fallback never lands on a draining host by guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillFallbackHost
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillFallbackHostRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillPortError

pytestmark = pytest.mark.unit

RANKED = ("a", "b", "c")
LIMITED = frozenset({"b"})


class FakePlacement:
    """A ``ProtocolLabFillPlacementReader`` whose limited markers are fixed."""

    def __init__(self, limited: frozenset[str] = frozenset()) -> None:
        self._limited = limited

    def read_pool(self) -> Sequence[Mapping[str, object]]:
        return ()

    def limited_hosts(self) -> frozenset[str]:
        return self._limited


class RaisingPlacement:
    """A placement reader whose marker read fails, as a broken runner read does."""

    def read_pool(self) -> Sequence[Mapping[str, object]]:
        raise LabFillPortError("EIO")

    def limited_hosts(self) -> frozenset[str]:
        raise LabFillPortError("EIO")


def test_fallback_skips_the_draining_host_and_lands_on_the_next_ranked() -> None:
    handler = HandlerLabFillFallbackHost(
        FakePlacement(limited=LIMITED),
        draining_hosts=lambda: ("a",),
    )
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == "c"
    )


def test_fallback_lands_on_the_first_ranked_host_when_nothing_is_draining() -> None:
    handler = HandlerLabFillFallbackHost(
        FakePlacement(limited=LIMITED),
        draining_hosts=lambda: (),
    )
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == "a"
    )


def test_fallback_offers_no_host_when_the_drain_state_cannot_be_read() -> None:
    def broken() -> tuple[str, ...]:
        raise OSError("the drain store is unreadable")

    handler = HandlerLabFillFallbackHost(
        FakePlacement(limited=LIMITED),
        draining_hosts=broken,
    )
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == ""
    )


def test_fallback_offers_no_host_when_the_limited_markers_cannot_be_read() -> None:
    # Existing behaviour kept: an unreadable marker read offered no host before the drain read joined.
    handler = HandlerLabFillFallbackHost(RaisingPlacement(), draining_hosts=lambda: ())
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == ""
    )


def test_control_without_the_injected_callable_the_drain_state_is_not_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The default drain read looks under ONEX_SKILL_OVERLAY_ROOTS: an empty root holds no
    # drain overlay, so no drain verb is deployed and no host can be draining.
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    draining: tuple[str, ...] = ("a",)
    without = HandlerLabFillFallbackHost(FakePlacement(limited=LIMITED))
    # 'a' is offered even though this test itself says a is draining: nothing told the
    # handler, because the default reads a state with no host draining.
    assert (
        without.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == "a"
    )
    # The injected callable is what changes the answer: the same ranked hosts now skip 'a'.
    injected = HandlerLabFillFallbackHost(
        FakePlacement(limited=LIMITED),
        draining_hosts=lambda: draining,
    )
    assert (
        injected.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host
        == "c"
    )
