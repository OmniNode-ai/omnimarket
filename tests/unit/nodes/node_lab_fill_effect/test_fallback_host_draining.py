# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill fallback host skips a draining host, read live like the limited markers (OMN-24489).

The request's ``draining_hosts`` names the hosts whose capacity advertisements carry a live drain
mark, so a host the drain verb is draining is never pinned. A fallback never lands on a draining
host by guess.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

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
    handler = HandlerLabFillFallbackHost(FakePlacement(limited=LIMITED))
    assert (
        handler.handle(
            ModelLabFillFallbackHostRequest(ranked_hosts=RANKED, draining_hosts=("a",))
        ).host
        == "c"
    )


def test_fallback_lands_on_the_first_ranked_host_when_nothing_is_draining() -> None:
    handler = HandlerLabFillFallbackHost(FakePlacement(limited=LIMITED))
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == "a"
    )


def test_fallback_offers_no_host_when_the_limited_markers_cannot_be_read() -> None:
    # Existing behaviour kept: an unreadable marker read offered no host before the drain read joined.
    handler = HandlerLabFillFallbackHost(RaisingPlacement())
    assert (
        handler.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == ""
    )


def test_control_without_the_field_the_drain_state_is_not_seen() -> None:
    # The request's ``draining_hosts`` is what changes the answer: the same ranked hosts now skip 'a'.
    without = HandlerLabFillFallbackHost(FakePlacement(limited=LIMITED))
    # 'a' is offered even though this test itself says a is draining: nothing told the
    # handler, because the request names no draining host.
    assert (
        without.handle(ModelLabFillFallbackHostRequest(ranked_hosts=RANKED)).host == "a"
    )
    # The request's field is what changes the answer: the same ranked hosts now skip 'a'.
    injected = HandlerLabFillFallbackHost(FakePlacement(limited=LIMITED))
    assert (
        injected.handle(
            ModelLabFillFallbackHostRequest(ranked_hosts=RANKED, draining_hosts=("a",))
        ).host
        == "c"
    )
