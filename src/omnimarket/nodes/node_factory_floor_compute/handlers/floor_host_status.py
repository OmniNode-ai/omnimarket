# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Machines tab's per-host record: one status per host of the host table, every value from its own source (OMN-22673).

A value has one of three states. ``read`` is a number inside its source's freshness bound. ``stale`` keeps the number and
says how old it is. ``unread`` has no number and says why: the source was not read, holds no row for the host, or has no
cell for the value. Nothing here guesses a value or turns a missing source into a healthy one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorHostRow,
    ModelFloorPoolReading,
)
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    HostCondition,
    ModelFloorHostStatus,
    ModelFloorLane,
    ModelHostFlag,
    ModelHostLaneUse,
    ModelHostNumber,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(now: datetime, then: datetime) -> int:
    return max(0, int((now - then).total_seconds()))


def _bound_text(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def unread_number(unit: str, source: str, reason: str) -> ModelHostNumber:
    return ModelHostNumber(
        state="unread",
        value=None,
        unit=unit,
        source=source,
        read_at="",
        age_min=None,
        reason=reason,
    )


def unread_flag(source: str, reason: str) -> ModelHostFlag:
    return ModelHostFlag(
        state="unread",
        value=None,
        source=source,
        read_at="",
        age_min=None,
        reason=reason,
    )


def _judge(
    now: datetime, read_at: datetime, bound_seconds: int, source: str
) -> tuple[str, int, str]:
    """The state a reading of ``source`` takes at ``now``, its age in minutes and the reason when it is stale."""
    age = _seconds(now, read_at)
    if age > bound_seconds:
        return (
            "stale",
            age // 60,
            f"{source} was read {age // 60} min ago; its bound is {_bound_text(bound_seconds)}",
        )
    return "read", age // 60, ""


def number(
    value: float,
    unit: str,
    source: str,
    read_at: datetime,
    now: datetime,
    bound_seconds: int,
) -> ModelHostNumber:
    state, age_min, reason = _judge(now, read_at, bound_seconds, source)
    return ModelHostNumber(
        state="stale" if state == "stale" else "read",
        value=round(value, 2),
        unit=unit,
        source=source,
        read_at=_stamp(read_at),
        age_min=age_min,
        reason=reason,
    )


def flag(
    value: bool,
    source: str,
    read_at: datetime,
    now: datetime,
    bound_seconds: int,
) -> ModelHostFlag:
    state, age_min, reason = _judge(now, read_at, bound_seconds, source)
    return ModelHostFlag(
        state="stale" if state == "stale" else "read",
        value=value,
        source=source,
        read_at=_stamp(read_at),
        age_min=age_min,
        reason=reason,
    )


class _PoolCells:
    """The pool reading's cells of one host, each a number or the reason it has none."""

    POOL = "pool read"

    def __init__(
        self,
        request: ModelFactoryFloorRequest,
        host: str,
        row: ModelFloorHostRow | None,
    ) -> None:
        self._now = request.generated_at
        self._bound = request.config.pool_stale_minutes * 60
        self._disk_cell = request.config.host_status.disk_cell
        self.reading: ModelFloorPoolReading | None = None
        self.why = ""
        readings = {r.host: r for r in request.pool or ()}
        if request.pool is None:
            self.why = "the pool read was not recorded"
        elif host not in readings:
            out = (
                " (the host table has it out of selection, so no pool read is asked of it)"
                if row is not None and not row.available
                else ""
            )
            self.why = f"the pool read holds no row for this host{out}"
        elif readings[host].error:
            self.why = (
                f"the pool read could not read this host: {readings[host].error[:120]}"
            )
        else:
            self.reading = readings[host]

    def cell(self, value: float | None, unit: str, missing: str) -> ModelHostNumber:
        if self.reading is None:
            return unread_number(unit, self.POOL, self.why)
        if value is None:
            return unread_number(
                unit, self.POOL, f"the pool reading carries no {missing}"
            )
        return number(
            value, unit, self.POOL, self.reading.read_at, self._now, self._bound
        )

    def load_per_core(self) -> ModelHostNumber:
        r = self.reading
        value = (
            r.load1 / r.cores
            if r is not None and r.load1 is not None and r.cores
            else None
        )
        return self.cell(value, "per core", "load or core count cell")

    def pressure(self) -> ModelHostNumber:
        r = self.reading
        if r is None or r.mem_pressure is None:
            return self.cell(None, "", "memory pressure cell (mempsi or pressure)")
        unit = "psi" if r.mem_pressure_kind == "psi" else "level"
        return self.cell(r.mem_pressure, unit, "memory pressure cell")

    def disk(self) -> tuple[ModelHostNumber, ModelHostNumber]:
        r = self.reading
        picked = (
            next((d for d in r.disks if d.name == self._disk_cell), None) if r else None
        )
        if picked is None:
            miss = f"disk_{self._disk_cell} cell"
            return self.cell(None, "GB", miss), self.cell(None, "% used", miss)
        used = 100.0 * (picked.total_gb - picked.free_gb) / picked.total_gb
        return (
            self.cell(picked.free_gb, "GB", "disk cell"),
            self.cell(used, "% used", "disk cell"),
        )


def _advert(
    request: ModelFactoryFloorRequest, host: str
) -> ModelHostCapacityAdvertisement | None:
    return next((a for a in request.placement if a.host_name == host), None)


def _docker(request: ModelFactoryFloorRequest, host: str) -> ModelHostFlag:
    source = "capacity advertisement"
    cfg = request.config.host_status
    ad = _advert(request, host)
    if ad is None:
        return unread_flag(
            source,
            "no capacity advertisement from this host"
            + (
                " (the advertisement file holds no rows)"
                if not request.placement
                else ""
            ),
        )
    return flag(
        cfg.docker_tool in ad.tools,
        source,
        ad.advertised_at,
        request.generated_at,
        cfg.advert_stale_minutes * 60,
    )


def _heartbeat(
    request: ModelFactoryFloorRequest, host: str
) -> tuple[ModelHostNumber, ModelHostNumber]:
    source = "host-drift fleet projection"
    cfg = request.config.host_status
    drift = request.host_drift
    if drift is None:
        why = request.host_drift_cause or "the host-drift fleet projection was not read"
        return unread_number("min", source, why), unread_number("items", source, why)
    now = request.generated_at
    fold_age = _seconds(now, drift.folded_at)
    row = next((h for h in drift.hosts if h.host == host), None)
    if row is None:
        why = "the host-drift fleet projection holds no row for this host"
        return unread_number("min", source, why), unread_number("items", source, why)
    # A projection folded too long ago may hide a newer heartbeat, so its figures are stale whatever the heartbeat says.
    if fold_age > cfg.heartbeat_stale_seconds:
        bound = _bound_text(cfg.heartbeat_stale_seconds)
        why = f"the fleet projection was folded {fold_age // 60} min ago; its bound is {bound}"
        beat = (
            ModelHostNumber(
                state="stale",
                value=round(_seconds(now, row.last_heartbeat_at) / 60, 2),
                unit="min",
                source=source,
                read_at=_stamp(drift.folded_at),
                age_min=fold_age // 60,
                reason=why,
            )
            if row.last_heartbeat_at is not None
            else unread_number(
                "min", source, "this host has never reported a heartbeat"
            )
        )
        items = ModelHostNumber(
            state="stale",
            value=float(row.open_items),
            unit="items",
            source=source,
            read_at=_stamp(drift.folded_at),
            age_min=fold_age // 60,
            reason=why,
        )
        return beat, items
    items = number(
        float(row.open_items),
        "items",
        source,
        drift.folded_at,
        now,
        cfg.heartbeat_stale_seconds,
    )
    if row.last_heartbeat_at is None:
        return unread_number(
            "min", source, "this host has never reported a heartbeat"
        ), items
    # The figure is the heartbeat's own age, judged against the heartbeat bound: an old heartbeat is stale, not healthy.
    beat_age = _seconds(now, row.last_heartbeat_at)
    beat_state = "stale" if beat_age > cfg.heartbeat_stale_seconds else "read"
    beat = ModelHostNumber(
        state=beat_state,
        value=round(beat_age / 60, 2),
        unit="min",
        source=source,
        read_at=_stamp(row.last_heartbeat_at),
        age_min=beat_age // 60,
        reason=""
        if beat_state == "read"
        else f"the newest heartbeat is {beat_age // 60} min old; its bound is {_bound_text(cfg.heartbeat_stale_seconds)}",
    )
    return beat, items


def _parked_heartbeat(
    request: ModelFactoryFloorRequest, host: str, row: ModelFloorHostRow | None
) -> tuple[ModelHostNumber, ModelHostNumber]:
    """The heartbeat cells of one host, reading the hosts the table parks before the projection.

    A host the table parks — ``available: false`` and not ``local`` — expects no heartbeat, so both
    cells are unread with the table's own reason rather than the ``never reported a heartbeat``
    failure of an unread machine. The local row is the machine the publisher runs on: it keeps
    sending heartbeats whatever its placement flag says, so it and every other host are judged by
    ``_heartbeat`` as before.
    """
    if row is None or row.available or row.local:
        return _heartbeat(request, host)
    source = "host-drift fleet projection"
    why = (
        f"out of selection ({row.unavailable_reason or 'no reason in the host table'}); "
        "no heartbeat is expected from this host"
    )
    return unread_number("min", source, why), unread_number("items", source, why)


def _use(lane: ModelFloorLane) -> ModelHostLaneUse:
    return ModelHostLaneUse(
        lane=lane.lane,
        ticket=lane.ticket,
        kind=lane.kind,
        engine=lane.engine,
        started_at=lane.started_at,
        run_minutes=lane.run_minutes,
        stalled=lane.stalled,
    )


def _hosts(
    request: ModelFactoryFloorRequest,
) -> tuple[tuple[str, str, ModelFloorHostRow | None], ...]:
    """Every host of the host table, with the label the overlay gives a drawn machine; the declared machines when the table is unread."""
    labels = {m.host: m.label for m in request.machines}
    if request.host_table is None:
        return tuple((m.host, m.label, None) for m in request.machines)
    return tuple(
        (row.host, labels.get(row.host, row.host), row)
        for row in sorted(request.host_table, key=lambda r: r.host)
    )


def _condition(
    row: ModelFloorHostRow | None,
    pool: _PoolCells,
    age: ModelHostNumber,
    refusal: str,
) -> HostCondition:
    """Out, unread, stale, refusing or read. There is no healthy: a host whose reading is not fresh is never called read."""
    if row is not None and not row.available:
        return "out"
    if pool.reading is None:
        return "unread"
    if age.state == "stale":
        return "stale"
    if refusal:
        return "refusing"
    return "read"


def build_host_status(
    request: ModelFactoryFloorRequest, lanes: tuple[ModelFloorLane, ...]
) -> tuple[ModelFloorHostStatus, ...]:
    now, cfg = request.generated_at, request.config.host_status
    placed = {p.host: p for p in request.placed or ()}
    out: list[ModelFloorHostStatus] = []
    for host, label, row in _hosts(request):
        pool = _PoolCells(request, host, row)
        table = "host table"
        if row is None:
            why = (
                "the host table was not read"
                if request.host_table is None
                else "the host table holds no row for this host"
            )
            available, reason = unread_flag(table, why), ""
        else:
            # The table carries no read time of its own: it is read live on every publish cycle.
            available = ModelHostFlag(
                state="read",
                value=row.available,
                source=table,
                read_at=_stamp(now),
                age_min=0,
                reason="",
            )
            reason = row.unavailable_reason
        cap_value = row.cap if row is not None and row.cap is not None else None
        if cap_value is not None:
            cap = number(float(cap_value), "slots", table, now, now, 1)
        else:
            pool_cap = pool.reading.cap if pool.reading is not None else None
            cap = (
                pool.cell(float(pool_cap), "slots", "cap cell")
                if pool_cap is not None
                else unread_number(
                    "slots",
                    table,
                    "neither the host table nor the pool reading gives a cap",
                )
            )
        occupants = tuple(
            lane for lane in lanes if lane.placement == "machine" and lane.host == host
        )
        landing = tuple(
            _use(lane) for lane in occupants if lane.kind == cfg.landing_worker_kind
        )
        mine = tuple(
            _use(lane) for lane in occupants if lane.kind != cfg.landing_worker_kind
        )
        marker = placed.get(host)
        markers = "slot markers"
        if request.placed is None:
            why = "the slot markers were not read"
            used = unread_number("slots", markers, why)
            other = unread_number("units", markers, why)
        else:
            units = marker.units if marker is not None else 0
            remote = marker.lanes if marker is not None else 0
            used = number(float(units), "slots", markers, now, now, 1)
            other = number(float(max(0, units - remote)), "units", markers, now, now, 1)
        reading = pool.reading
        if reading is None:
            free = unread_number("slots", pool.POOL, pool.why)
        else:
            # The reading's lane_slots cell, else its slots cell: the free count the landing placement printed.
            free = pool.cell(
                float(reading.slots) if reading.slots is not None else None,
                "slots",
                "slots cell",
            )
        load = pool.load_per_core()
        disk_free, disk_used = pool.disk()
        beat, drift_open = _parked_heartbeat(request, host, row)
        age = (
            number(
                _seconds(now, reading.read_at) / 60,
                "min",
                pool.POOL,
                reading.read_at,
                now,
                request.config.pool_stale_minutes * 60,
            )
            if reading is not None
            else unread_number("min", pool.POOL, pool.why)
        )
        refusal = reading.refusal if reading is not None else ""
        out.append(
            ModelFloorHostStatus(
                host=host,
                label=label,
                condition=_condition(row, pool, age, refusal),
                available=available,
                unavailable_reason=reason,
                cap=cap,
                slots_used=used,
                slots_free=free,
                admission_refusal=refusal,
                lanes=mine,
                landing_workers=landing,
                other_units=other,
                load_per_core=load,
                mem_free_gb=pool.cell(
                    reading.mem_free_gb if reading else None, "GB", "free memory cell"
                ),
                mem_pressure=pool.pressure(),
                swap_used_pct=pool.cell(
                    reading.swap_used_pct if reading else None, "%", "swap cell"
                ),
                disk_free_gb=disk_free,
                disk_used_pct=disk_used,
                heartbeat_age_min=beat,
                drift_open=drift_open,
                docker=_docker(request, host),
                reading_age=age,
            )
        )
    return tuple(out)
