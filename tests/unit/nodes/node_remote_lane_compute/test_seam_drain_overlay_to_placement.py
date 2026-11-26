# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain mark reaches the remote-lane placement node by the advertisement alone (OMN-24489).

A pool host advertises its drain mark on its capacity advertisement as the ``draining-until:``
pseudo-tool; the pool snapshot turns that mark into the reading's ``lane_admission_refusal``, and the
placement node refuses a reading that carries it. This seam builds advertisements with and without the
mark, runs ``build_pool_snapshot`` to make the readings, feeds them to ``HandlerRemoteLanePlacement``,
and asserts the marked host is refused with a reason containing ``draining`` while the other host
places; an expired mark places again. It fails when either side changes shape: the advertisement's
``tools`` key, the snapshot's refusal, or the placement's reading key.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.models.remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    build_pool_snapshot,
    default_overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models import (
    ModelHostAccounts,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)

T0 = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)


def _ad(
    host: str,
    *,
    tools: tuple[str, ...] = (),
    load1: float = 1.0,
    advertised_at: datetime = T0,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=8,
        load1=load1,
        mem_available_bytes=32 * 2**30,
        advertised_at=advertised_at,
        cadence_seconds=15,
        max_units=4,
        capacity_refusal="",
        tools=tools,
    )


def _row(name: str) -> ModelLabHostRow:
    return ModelLabHostRow(
        name=name,
        target=name,
        available=True,
        unavailable_reason=None,
        cap=None,
        local=False,
        contributor=False,
        lane_ready=False,
        codex=False,
        fallback_targets=(),
    )


def _readings(
    ads: tuple[ModelHostCapacityAdvertisement, ...],
    read_at: datetime,
) -> tuple[ModelRemoteLaneHostReading, ...]:
    snapshot, _ = build_pool_snapshot(
        rows=(_row("example-host-a"), _row("example-host-b")),
        ads=ads,
        lanes={},
        accounts=(
            ModelHostAccounts(
                host="example-host-a",
                usable=("acct-a",),
                limited=(),
                unread_reason=None,
            ),
            ModelHostAccounts(
                host="example-host-b",
                usable=("acct-a",),
                limited=(),
                unread_reason=None,
            ),
        ),
        overlay=default_overlay(),
        read_at=read_at,
    )
    return tuple(
        ModelRemoteLaneHostReading.model_validate(
            {
                "name": r.name,
                "engines": r.engines,
                "lane_slots": r.lane_slots,
                "lane_cap": r.lane_cap,
                "rank_free": r.rank_free if r.rank_free is not None else 0.0,
                "lane_admission_refusal": r.lane_admission_refusal,
            }
        )
        for r in snapshot.readings
    )


def _request(
    readings: tuple[ModelRemoteLaneHostReading, ...],
) -> ModelRemoteLanePlacementRequest:
    # The snapshot's reading carries no engines: the placement reader's own reading does, so the seam
    # supplies the lane's engine to keep the host eligible for the lane it is being placed on.
    return ModelRemoteLanePlacementRequest(
        engine="claude_sonnet",
        readings=tuple(
            r.model_copy(update={"engines": ("claude_sonnet",)}) for r in readings
        ),
    )


@pytest.mark.unit
def test_a_live_drain_mark_is_refused_by_the_placement_and_the_other_places() -> None:
    # No mark: the freest host takes the lane.
    clean = _readings((_ad("example-host-a", load1=0.5), _ad("example-host-b")), T0)
    assert HandlerRemoteLanePlacement().handle(_request(clean)).host == "example-host-a"

    # A live mark: the snapshot turns it into a refusal and the placement refuses the marked host.
    # The advertisement is fresh (read at T0) so the snapshot does not refuse it as stale.
    marked = _readings(
        (
            _ad(
                "example-host-a",
                load1=0.5,
                tools=("draining-until:2026-11-11T04:00:00Z",),
            ),
            _ad("example-host-b"),
        ),
        T0,
    )
    result = HandlerRemoteLanePlacement().handle(_request(marked))
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "example-host-b"
    reasons = {v.host: v.reason for v in result.verdicts}
    assert "draining" in reasons["example-host-a"]


@pytest.mark.unit
def test_an_expired_drain_mark_places_again() -> None:
    # The mark expired before the read: the snapshot gives no drain refusal and the host places.
    # The advertisement is fresh (read at T0) so the snapshot does not refuse it as stale; only the
    # drain mark is past its expiry, which is what this control isolates.
    expired = _readings(
        (
            _ad(
                "example-host-a",
                load1=0.5,
                tools=("draining-until:2026-11-11T02:00:00Z",),
            ),
            _ad("example-host-b"),
        ),
        T0,
    )
    assert (
        HandlerRemoteLanePlacement().handle(_request(expired)).host == "example-host-a"
    )
