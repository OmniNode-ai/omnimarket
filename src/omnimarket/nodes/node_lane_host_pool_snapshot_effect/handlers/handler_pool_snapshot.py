# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot step of the lane start and floor fill workflow (OMN-23664).

One tick: read the host advertisements, host table, running lanes, and accounts from the
declared sources, build the pool snapshot and record, and write them to the store.
A source that cannot read ends in the typed failed event and replaces no earlier record.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)
from pydantic import BaseModel

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow
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
    ModelHostAccounts,
    ModelPoolHostRow,
    ModelPoolSnapshotFailedEvent,
    ModelPoolSnapshotRecord,
    ModelPoolSnapshotRequest,
    ModelPoolSnapshotResult,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    ModelPoolSnapshotOverlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.protocols.protocol_pool_snapshot import (
    ProtocolHostAccountsSource,
    ProtocolHostAdvertisementSource,
    ProtocolHostTableSource,
    ProtocolPoolSnapshotPublisher,
    ProtocolPoolSnapshotStore,
    ProtocolRunningLanesSource,
)

_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "overlay.example.yaml"


def default_overlay() -> ModelPoolSnapshotOverlay:
    """The node's shipped example overlay: the in-memory default when none is injected."""
    return ModelPoolSnapshotOverlay.model_validate(
        yaml.safe_load(_OVERLAY_PATH.read_text(encoding="utf-8"))
    )


def _age_seconds(ad: ModelHostCapacityAdvertisement, read_at: datetime) -> float:
    """Seconds between the advertisement and the read time."""
    return (read_at - ad.advertised_at).total_seconds()


def build_pool_snapshot(
    rows: tuple[ModelLabHostRow, ...],
    ads: tuple[ModelHostCapacityAdvertisement, ...],
    lanes: dict[str, int],
    accounts: tuple[ModelHostAccounts, ...],
    overlay: ModelPoolSnapshotOverlay,
    read_at: datetime,
) -> tuple[ModelLaneHostPoolSnapshot, tuple[ModelPoolHostRow, ...]]:
    """Build the pool snapshot and per-host rows from the read sources."""
    ad_by_host: dict[str, ModelHostCapacityAdvertisement] = {}
    for candidate in ads:
        held = ad_by_host.get(candidate.host_name)
        if held is None or candidate.advertised_at > held.advertised_at:
            ad_by_host[candidate.host_name] = candidate
    acc_by_host = {a.host: a for a in accounts}

    host_rows: list[ModelPoolHostRow] = []
    readings: list[ModelLaneHostPoolReading] = []

    for row in sorted(rows, key=lambda r: r.name):
        ad: ModelHostCapacityAdvertisement | None = ad_by_host.get(row.name)
        acc = acc_by_host.get(row.name)
        accounts_usable = acc.usable if acc else ()
        accounts_limited = acc.limited if acc else ()

        away = not row.available
        away_reason = (row.unavailable_reason or "unavailable") if away else None

        if row.cap is not None:
            declared_slots = row.cap
        elif ad is not None:
            declared_slots = max(1, ad.cores // overlay.cores_per_lane)
        else:
            declared_slots = 0

        running_lanes = lanes.get(row.name, 0)

        refusal: str | None = None
        if away:
            refusal = f"away: {away_reason}"
        elif ad is None:
            refusal = "no-advertisement"
        elif _age_seconds(ad, read_at) > overlay.advertisement_max_age_seconds:
            refusal = f"advertisement-stale: {int(_age_seconds(ad, read_at))}s > {overlay.advertisement_max_age_seconds}s"
        elif ad.capacity_refusal:
            refusal = f"capacity: {ad.capacity_refusal}"
        elif acc and acc.unread_reason:
            refusal = f"accounts-unread: {acc.unread_reason}"
        elif not accounts_usable:
            refusal = "no-usable-account"
        elif row.contributor and not row.lane_ready:
            refusal = f"lane-not-ready: {row.lane_not_ready_reason or 'not ready'}"

        if refusal is not None:
            free_slots = 0
        else:
            free_slots = max(0, declared_slots - running_lanes)

        host_rows.append(
            ModelPoolHostRow(
                host=row.name,
                declared_slots=declared_slots,
                running_lanes=running_lanes,
                free_slots=free_slots,
                accounts_usable=accounts_usable,
                accounts_limited=accounts_limited,
                away=away,
                away_reason=away_reason,
                refusal=refusal,
                advertised_at=ad.advertised_at if ad else None,
            )
        )

        engines = overlay.engines
        if row.codex:
            engines = (*engines, overlay.codex_engine)
        if not accounts_usable:
            engines = ()

        if (
            ad is None
            or _age_seconds(ad, read_at) > overlay.advertisement_max_age_seconds
        ):
            rank_free = None
            mem_avail_gb = 0.0
        else:
            # Free cores less the table penalty, the rank the old runner's reading gave a host.
            rank_free = ad.cores - ad.load1 - row.penalty * ad.cores
            mem_avail_gb = ad.mem_available_bytes / 2**30

        readings.append(
            ModelLaneHostPoolReading(
                name=row.name,
                target=row.target,
                local=row.local,
                engines=engines,
                lane_admission_refusal=refusal,
                codex_refusal=None,
                lane_slots=free_slots,
                lane_cap=declared_slots,
                placed=running_lanes,
                rank_free=rank_free,
                mem_avail_gb=mem_avail_gb,
                contributor=row.contributor,
                bus_lane=row.lane_binding or row.ledger_bus_lane,
                lane_ready=row.lane_ready or not row.contributor,
                lane_not_ready_reason=row.lane_not_ready_reason,
            )
        )

    limited_hosts = tuple(
        r.host for r in host_rows if r.accounts_limited and not r.accounts_usable
    )
    snapshot = ModelLaneHostPoolSnapshot(
        readings=tuple(readings),
        limited_hosts=limited_hosts,
        auth_expired_hosts=(),
        codex_limited_hosts=(),
        launching_venv_hash=None,
    )
    return snapshot, tuple(host_rows)


class HandlerPoolSnapshot:
    """Read the pool sources, build the snapshot, and write it to the store.

    Every port is injected in tests and by the composed workflow. Without one,
    the handler builds the in-memory default: the shipped example overlay and
    the empty in-memory ports.
    """

    def __init__(
        self,
        *,
        overlay: ModelPoolSnapshotOverlay | None = None,
        advertisement_source: ProtocolHostAdvertisementSource | None = None,
        host_table_source: ProtocolHostTableSource | None = None,
        lanes_source: ProtocolRunningLanesSource | None = None,
        accounts_source: ProtocolHostAccountsSource | None = None,
        store: ProtocolPoolSnapshotStore | None = None,
        publisher: ProtocolPoolSnapshotPublisher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._overlay = default_overlay() if overlay is None else overlay
        self._advertisement_source = (
            InMemoryHostAdvertisementSource(())
            if advertisement_source is None
            else advertisement_source
        )
        self._host_table_source = (
            InMemoryHostTableSource(())
            if host_table_source is None
            else host_table_source
        )
        self._lanes_source = (
            InMemoryRunningLanesSource({}) if lanes_source is None else lanes_source
        )
        self._accounts_source = (
            InMemoryHostAccountsSource(())
            if accounts_source is None
            else accounts_source
        )
        self._store = InMemoryPoolSnapshotStore() if store is None else store
        self._publisher = (
            InMemoryPoolSnapshotPublisher() if publisher is None else publisher
        )
        self._clock = clock

    def handle(self, request: ModelPoolSnapshotRequest) -> ModelPoolSnapshotResult:
        read_at = self._clock()

        try:
            ads = self._advertisement_source.read_advertisements()
        except PoolSnapshotSourceError as exc:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.ADVERTISEMENTS_UNREAD, str(exc)
            )

        try:
            rows = self._host_table_source.read_hosts()
        except PoolSnapshotSourceError as exc:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.HOST_TABLE_UNREAD, str(exc)
            )

        if not rows:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.NO_HOSTS, "host table is empty"
            )

        try:
            lanes = self._lanes_source.running_lanes(read_at)
        except PoolSnapshotSourceError as exc:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.LANES_UNREAD, str(exc)
            )

        try:
            accounts = self._accounts_source.read_accounts(rows)
        except PoolSnapshotSourceError as exc:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.ACCOUNTS_UNREAD, str(exc)
            )

        snapshot, host_rows = build_pool_snapshot(
            rows, ads, lanes, accounts, self._overlay, read_at
        )

        record = ModelPoolSnapshotRecord(
            correlation_id=request.correlation_id,
            read_at=read_at,
            max_age_seconds=self._overlay.snapshot_max_age_seconds,
            hosts=host_rows,
            free_slots_total=sum(r.free_slots for r in host_rows),
            snapshot_path=self._overlay.snapshot_path or "",
            provenance=" + ".join(
                [
                    self._advertisement_source.name,
                    self._host_table_source.name,
                    self._lanes_source.name,
                    self._accounts_source.name,
                ]
            ),
        )

        if not request.write:
            return ModelPoolSnapshotResult(
                correlation_id=request.correlation_id,
                status=EnumPoolSnapshotStatus.OBSERVED,
                record=record,
            )

        try:
            self._store.write(snapshot, record)
        except PoolSnapshotSourceError as exc:
            return self._fail(
                request, EnumPoolSnapshotFailureReason.WRITE_FAILED, str(exc)
            )

        unpublished = self._publish(self._overlay.snapshot_topic, record)
        if unpublished is not None:
            return self._fail(
                request,
                EnumPoolSnapshotFailureReason.PUBLISH_FAILED,
                f"the snapshot was written but its record was not published: {unpublished}",
            )
        return ModelPoolSnapshotResult(
            correlation_id=request.correlation_id,
            status=EnumPoolSnapshotStatus.WRITTEN,
            record=record,
        )

    def _publish(self, topic: str, event: BaseModel) -> str | None:
        """Publish through the injected transport; the error text when it refuses, else None."""
        try:
            self._publisher.publish(topic, event)
        except Exception as exc:  # the transport's errors are its own types: any refusal is a typed failure here
            return f"{type(exc).__name__}: {exc}"
        return None

    def _fail(
        self,
        request: ModelPoolSnapshotRequest,
        reason: EnumPoolSnapshotFailureReason,
        detail: str,
    ) -> ModelPoolSnapshotResult:
        if request.write:
            unpublished = self._publish(
                self._overlay.failed_topic,
                ModelPoolSnapshotFailedEvent(
                    correlation_id=request.correlation_id,
                    reason=reason,
                    detail=detail,
                    read_at=self._clock(),
                ),
            )
            if unpublished is not None:
                detail = f"{detail} (the failed event was not published: {unpublished})"
        return ModelPoolSnapshotResult(
            correlation_id=request.correlation_id,
            status=EnumPoolSnapshotStatus.FAILED,
            failure_reason=reason,
            failure_detail=detail,
        )


__all__: list[str] = [
    "HandlerPoolSnapshot",
    "build_pool_snapshot",
    "default_overlay",
]
