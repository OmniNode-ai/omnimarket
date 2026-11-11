# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot step: one pass projects the per-host rows and writes the record (OMN-23664).

Golden: four hosts each exercise one refusal path; the written snapshot file validates as a
ModelLaneHostPoolSnapshot and the record carries a tz-aware read_at and the per-host projections.
Error: each source raising PoolSnapshotSourceError ends in the matching failed event and writes nothing.
Failing controls: a naive read_at is refused by the model, a handler that ignores row.available gives
the away host a free slot, and write=False observes without touching the store or publisher.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omnibase_spi.contracts.services import ModelLaneHostPoolSnapshot
from pydantic import ValidationError

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    build_pool_snapshot,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    EnumPoolSnapshotFailureReason,
    EnumPoolSnapshotStatus,
    ModelPoolSnapshotFailedEvent,
    ModelPoolSnapshotRecord,
    ModelPoolSnapshotRequest,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    CORRELATION,
    T0,
    accounts,
    advertisement,
    handler,
    host_row,
    overlay,
    pool_request,
)

pytestmark = pytest.mark.unit


def _golden_inputs():
    ads = (
        advertisement("h1"),
        advertisement("h2"),
        advertisement("h3"),
        advertisement("h4", advertised_at=datetime(2026, 11, 11, 2, 0, tzinfo=UTC)),
    )
    rows = (
        host_row("h1"),
        host_row("h2", available=False, unavailable_reason="repair"),
        host_row("h3"),
        host_row("h4"),
    )
    lanes = {"h1": 1, "h2": 0, "h3": 0, "h4": 0}
    accs = (
        accounts("h1"),
        accounts("h2"),
        accounts("h3", usable=(), limited=("acct-a",)),
        accounts("h4"),
    )
    return ads, rows, lanes, accs


def test_golden_four_hosts_project_their_refusals_and_the_snapshot_file_validates() -> (
    None
):
    ads, rows, lanes, accs = _golden_inputs()
    store = InMemoryPoolSnapshotStore()
    publisher = InMemoryPoolSnapshotPublisher()
    h = handler(
        ads=ads,
        rows=rows,
        lanes=lanes,
        accs=accs,
        store=store,
        publisher=publisher,
        at=T0,
    )
    result = h.handle(pool_request())
    assert result.status is EnumPoolSnapshotStatus.WRITTEN
    record = result.record
    assert record is not None
    assert record.read_at.tzinfo is not None
    assert record.read_at == T0
    assert record.correlation_id == CORRELATION

    by_host = {row.host: row for row in record.hosts}
    h1, h2, h3, h4 = by_host["h1"], by_host["h2"], by_host["h3"], by_host["h4"]

    assert h1.free_slots == 3
    assert h1.refusal is None
    assert h2.free_slots == 0
    assert (h2.refusal or "").startswith("away")
    assert h3.refusal == "no-usable-account"
    assert h3.free_slots == 0
    assert (h4.refusal or "").startswith("advertisement-stale")
    assert h4.free_slots == 0

    assert record.free_slots_total == 3
    assert h3.accounts_limited == ("acct-a",)
    assert h3.accounts_usable == ()

    snapshot = store.snapshot
    assert snapshot is not None
    parsed = ModelLaneHostPoolSnapshot.model_validate_json(snapshot.model_dump_json())
    assert parsed == snapshot

    readings = {r.name: r for r in parsed.readings}
    for name, held in by_host.items():
        reading = readings[name]
        assert reading.lane_slots == held.free_slots
        assert reading.placed == held.running_lanes
        assert reading.lane_cap == held.declared_slots

    assert parsed.limited_hosts == ("h3",)
    assert [topic for topic, _ in publisher.published] == [overlay().snapshot_topic]
    assert isinstance(publisher.published[0][1], ModelPoolSnapshotRecord)


def test_error_each_source_error_ends_failed_with_the_matching_reason() -> None:
    ads, rows, lanes, accs = _golden_inputs()
    expected = (
        (
            EnumPoolSnapshotFailureReason.ADVERTISEMENTS_UNREAD,
            lambda: InMemoryHostAdvertisementSource(ads),
        ),
        (
            EnumPoolSnapshotFailureReason.HOST_TABLE_UNREAD,
            lambda: InMemoryHostTableSource(rows),
        ),
        (
            EnumPoolSnapshotFailureReason.LANES_UNREAD,
            lambda: InMemoryRunningLanesSource(lanes),
        ),
        (
            EnumPoolSnapshotFailureReason.ACCOUNTS_UNREAD,
            lambda: InMemoryHostAccountsSource(accs),
        ),
    )
    for reason, source_factory in expected:
        store = InMemoryPoolSnapshotStore()
        publisher = InMemoryPoolSnapshotPublisher()
        h = handler(
            ads=ads,
            rows=rows,
            lanes=lanes,
            accs=accs,
            store=store,
            publisher=publisher,
            at=T0,
        )
        if reason is EnumPoolSnapshotFailureReason.ADVERTISEMENTS_UNREAD:
            h._advertisement_source = _RaisingSource(source_factory())
        elif reason is EnumPoolSnapshotFailureReason.HOST_TABLE_UNREAD:
            h._host_table_source = _RaisingSource(source_factory())
        elif reason is EnumPoolSnapshotFailureReason.LANES_UNREAD:
            h._lanes_source = _RaisingSource(source_factory())
        else:
            h._accounts_source = _RaisingSource(source_factory())

        result = h.handle(pool_request())
        assert result.status is EnumPoolSnapshotStatus.FAILED
        assert result.failure_reason is reason
        assert store.snapshot is None
        assert store.record is None
        assert len(publisher.published) == 1
        topic, event = publisher.published[0]
        assert topic == overlay().failed_topic
        assert isinstance(event, ModelPoolSnapshotFailedEvent)
        assert event.reason is reason


def test_failing_control_a_naive_read_at_is_refused_by_the_model() -> None:
    with pytest.raises(ValidationError):
        ModelPoolSnapshotRecord(
            correlation_id=CORRELATION,
            read_at=datetime(2026, 11, 11, 3, 0),
            max_age_seconds=180,
            hosts=(),
            free_slots_total=0,
            snapshot_path="",
            provenance="inmemory",
        )


def test_failing_control_dropping_the_away_mark_gives_h2_a_free_slot() -> None:
    """The same hosts with h2's row available give h2 free slots: only the row's availability keeps it at zero."""
    ads, rows, lanes, accs = _golden_inputs()
    _, away_rows = build_pool_snapshot(rows, ads, lanes, accs, overlay(), T0)
    assert next(r for r in away_rows if r.host == "h2").free_slots == 0
    assert next(r for r in away_rows if r.host == "h2").away is True

    dropped = tuple(host_row("h2") if row.name == "h2" else row for row in rows)
    _, open_rows = build_pool_snapshot(dropped, ads, lanes, accs, overlay(), T0)
    assert next(r for r in open_rows if r.host == "h2").free_slots == 4


def test_failing_control_write_false_observes_and_leaves_store_and_publisher_untouched() -> (
    None
):
    ads, rows, lanes, accs = _golden_inputs()
    store = InMemoryPoolSnapshotStore()
    publisher = InMemoryPoolSnapshotPublisher()
    h = handler(
        ads=ads,
        rows=rows,
        lanes=lanes,
        accs=accs,
        store=store,
        publisher=publisher,
        at=T0,
    )
    request = ModelPoolSnapshotRequest(correlation_id=CORRELATION, write=False)
    result = h.handle(request)
    assert result.status is EnumPoolSnapshotStatus.OBSERVED
    assert result.record is not None
    assert store.snapshot is None
    assert store.record is None
    assert store.writes == []
    assert publisher.published == []


class _RaisingSource:
    def __init__(self, inner) -> None:
        self._inner = inner

    def read_advertisements(self):
        raise PoolSnapshotSourceError("advertisements unreadable")

    def read_hosts(self):
        raise PoolSnapshotSourceError("host table unreadable")

    def running_lanes(self, now):
        raise PoolSnapshotSourceError("lanes unreadable")

    def read_accounts(self, hosts):
        raise PoolSnapshotSourceError("accounts unreadable")

    @property
    def name(self) -> str:
        return "raising-source"
