# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host whose accounts could not be read is unread in the snapshot, never zero free slots.

The fixture is a scrubbed copy of the readings the snapshot wrote on 2026-11-18, when five hosts
refused with an accounts-unread reason and the snapshot published ``free_slots_total=0`` while the
per-host headroom read held about 60 free slots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryPoolSnapshotStore,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    EnumPoolSnapshotStatus,
    ModelPoolSnapshotRecord,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    accounts,
    advertisement,
    handler,
    host_row,
    pool_request,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent / "fixtures" / "pool_readings_accounts_unread_2026-11-18.json"
)
UNREAD_HOSTS = ("h051", "h053", "h101", "h105", "h201")


def _fixture() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def _take(
    store: InMemoryPoolSnapshotStore | None = None, *, only: tuple[str, ...] = ()
) -> ModelPoolSnapshotRecord:
    recorded = _fixture()
    readings = [r for r in recorded["readings"] if not only or r["host"] in only]
    away = [] if only else recorded["away"]
    rows = tuple(host_row(r["host"], cap=r["lane_cap"]) for r in readings) + tuple(
        host_row(a["host"], available=False, unavailable_reason=a["reason"])
        for a in away
    )
    ads = tuple(advertisement(r["host"]) for r in readings) + tuple(
        advertisement(a["host"]) for a in away
    )
    accs = tuple(
        accounts(r["host"], usable=(), unread_reason=r["accounts_unread_reason"])
        if r["accounts_unread_reason"]
        else accounts(r["host"])
        for r in readings
    ) + tuple(accounts(a["host"], usable=()) for a in away)
    lanes = {r["host"]: r["placed"] for r in readings}
    result = handler(
        ads=ads, rows=rows, lanes=lanes, accs=accs, store=store, at=T0
    ).handle(pool_request())
    assert result.status is EnumPoolSnapshotStatus.WRITTEN
    assert result.record is not None
    return result.record


def test_unread_hosts_are_not_written_as_zero_free_slots() -> None:
    record = _take()
    by_host = {row.host: row for row in record.hosts}
    for name in UNREAD_HOSTS:
        row = by_host[name]
        assert row.free_slots is None, f"{name} was written with {row.free_slots}"
        assert row.accounts_unread is True
        assert (row.refusal or "").startswith(
            "accounts-unread: token pool command rc=3"
        )


def test_unread_hosts_add_nothing_to_the_total_and_are_named() -> None:
    record = _take()
    by_host = {row.host: row for row in record.hosts}
    assert record.unread_hosts == UNREAD_HOSTS
    # h202 was read and has headroom 1 (cap 3, 2 placed): the one host that adds to the total.
    assert by_host["h202"].free_slots == 1
    assert by_host["h202"].accounts_unread is False
    assert record.free_slots_total == 1
    # Away hosts stay a refusal with zero free slots: they are known, not unread.
    assert by_host["h106"].free_slots == 0
    assert by_host["h106"].accounts_unread is False
    assert (by_host["h200"].refusal or "").startswith("away")


def test_all_hosts_unread_total_is_zero_but_the_unread_hosts_are_named() -> None:
    record = _take(only=UNREAD_HOSTS)
    assert record.free_slots_total == 0
    assert record.unread_hosts == UNREAD_HOSTS


def test_unread_reading_publishes_the_marker_and_no_free_slot() -> None:
    store = InMemoryPoolSnapshotStore()
    _take(store)
    assert store.snapshot is not None
    readings = {r.name: r for r in store.snapshot.readings}
    for name in UNREAD_HOSTS:
        # The shared reading model holds an integer slot count; the marker is the admission refusal, which every
        # reader of the snapshot already treats as "no lane here", and the cap and placed counts stay true.
        assert (readings[name].lane_admission_refusal or "").startswith(
            "accounts-unread:"
        )
    assert readings["h051"].lane_cap == 32
    assert readings["h051"].placed == 1
    assert readings["h202"].lane_admission_refusal is None
    assert readings["h202"].lane_slots == 1
