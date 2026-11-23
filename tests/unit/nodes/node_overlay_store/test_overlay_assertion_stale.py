# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An overlay whose newest loader assertion is older than two hours alarms by id, with the party who acts on it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.overlay_store import (
    ModelOverlayAssertion,
    ModelOverlayAssertionCheck,
)
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayAssertionStale,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)


def assertion(overlay_id: str, age: timedelta) -> ModelOverlayAssertion:
    return ModelOverlayAssertion(
        overlay_id=overlay_id, digest="d" * 64, asserted_at=NOW - age
    )


def check(
    ids: tuple[str, ...], held: list[ModelOverlayAssertion], **extra: object
) -> ModelOverlayAssertionCheck:
    return ModelOverlayAssertionCheck(
        overlay_ids=ids,
        assertions=tuple(held),
        now=NOW,
        stale_after_s=7200,
        owner="overlay-store-owner",
        **extra,
    )


def test_an_id_older_than_two_hours_alarms_by_name_with_its_owner() -> None:
    result = HandlerOverlayAssertionStale().handle(
        check(
            ("lab_hosts", "lane_time_box"),
            [
                assertion("lab_hosts", timedelta(minutes=59)),
                assertion("lane_time_box", timedelta(hours=2, minutes=1)),
            ],
        )
    )
    assert [a.overlay_id for a in result.alarms] == ["lane_time_box"]
    alarm = result.alarms[0]
    assert alarm.owner == "overlay-store-owner"
    assert alarm.age_s == pytest.approx(7260)
    assert alarm.newest_assertion_at == NOW - timedelta(hours=2, minutes=1)
    assert alarm.bound_s == 7200
    assert result.fresh == ("lab_hosts",)


def test_an_id_with_no_assertion_alarms_and_says_so() -> None:
    result = HandlerOverlayAssertionStale().handle(check(("lab_hosts",), []))
    assert [a.overlay_id for a in result.alarms] == ["lab_hosts"]
    assert result.alarms[0].age_s is None
    assert "no assertion" in result.alarms[0].reason


def test_exactly_two_hours_is_not_yet_stale() -> None:
    result = HandlerOverlayAssertionStale().handle(
        check(("lab_hosts",), [assertion("lab_hosts", timedelta(hours=2))])
    )
    assert result.alarms == ()


def test_a_per_id_owner_wins_over_the_default() -> None:
    result = HandlerOverlayAssertionStale().handle(
        check(
            ("lab_hosts",),
            [assertion("lab_hosts", timedelta(hours=5))],
            owner_by_id={"lab_hosts": "lab-host-table-owner"},
        )
    )
    assert result.alarms[0].owner == "lab-host-table-owner"


def test_the_newest_of_several_assertions_decides() -> None:
    result = HandlerOverlayAssertionStale().handle(
        check(
            ("lab_hosts",),
            [
                assertion("lab_hosts", timedelta(hours=9)),
                assertion("lab_hosts", timedelta(minutes=5)),
            ],
        )
    )
    assert result.alarms == ()
