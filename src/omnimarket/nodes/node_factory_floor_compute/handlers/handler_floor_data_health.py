# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Data health of the factory floor record: one state per source, from the facts the caller already read.

Pure. The thresholds come from the request's config (the node contract); this module holds none. A source is:

- ``unavailable`` when it was never read or the last read failed (``read_at`` and ``age_min`` are None, and it is
  counted in ``summary.unread``, never as an age of 0);
- ``snapshot`` when the contract declares it a dated snapshot that does not refresh (a date-only basis stays a date);
- ``overdue`` when it was read longer ago than ``overdue_after_min``;
- ``partial`` when it was read but covered only some of what it should;
- ``available`` otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.model_factory_floor_input import ModelFactoryFloorRequest
from omnimarket.nodes.node_factory_floor_compute.models.model_floor_data_health import (
    ModelFloorDataHealth,
    ModelFloorDataHealthSummary,
    ModelFloorSourceHealth,
    SourceHealthState,
)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unavailable(source_id: str, note: str) -> ModelFloorSourceHealth:
    return ModelFloorSourceHealth(
        source_id=source_id,
        state="unavailable",
        read_at=None,
        observed_at=None,
        age_min=None,
        note=note,
    )


def _judge(
    request: ModelFactoryFloorRequest,
    source_id: str,
    *,
    read_at: datetime,
    observed_at: str | None = None,
    covered: tuple[int, int] | None = None,
    note: str = "",
) -> ModelFloorSourceHealth:
    """A source that was read at ``read_at``; ``covered`` is (parts read, parts expected) when it can be short."""
    settings = request.config.data_health
    age = max(0, int((request.generated_at - read_at).total_seconds() // 60))
    short = covered is not None and covered[0] < covered[1]
    detail = note
    if covered is not None and short:
        detail = f"{covered[0]} of {covered[1]} read" + (f"; {note}" if note else "")
    state: SourceHealthState
    if source_id in settings.snapshot_sources:
        basis = next(
            (b.basis for b in request.snapshot_bases if b.source_id == source_id), None
        )
        return ModelFloorSourceHealth(
            source_id=source_id,
            state="snapshot",
            read_at=_stamp(read_at),
            observed_at=basis,
            age_min=None,
            note=detail or ("" if basis else "snapshot basis not recorded"),
        )
    if age > settings.overdue_after_min:
        state = "overdue"
        detail = f"read {age} min ago, limit {settings.overdue_after_min}" + (
            f"; {detail}" if detail else ""
        )
    elif short:
        state = "partial"
    else:
        state = "available"
    return ModelFloorSourceHealth(
        source_id=source_id,
        state=state,
        read_at=_stamp(read_at),
        observed_at=observed_at,
        age_min=age,
        note=detail,
    )


def _pool(request: ModelFactoryFloorRequest) -> ModelFloorSourceHealth:
    if request.pool is None:
        return _unavailable("pool", "the lab pool reading was not read")
    good = tuple(r for r in request.pool if not r.error)
    if not good:
        return _unavailable("pool", "no host answered the pool reading")
    declared = {m.host for m in request.machines}
    seen = {r.host for r in good}
    expected = len(declared) if declared else len(request.pool)
    read = len(declared & seen) if declared else len(good)
    newest = max(r.read_at for r in good)
    return _judge(
        request,
        "pool",
        read_at=newest,
        observed_at=_stamp(newest),
        covered=(read, expected),
    )


def data_health(request: ModelFactoryFloorRequest) -> ModelFloorDataHealth:
    """One health row per source the record is computed from, and a summary with one count per state."""
    now = request.generated_at
    newest_row = max((row.ts for row in request.ledger), default=None)
    sources: list[ModelFloorSourceHealth] = [
        _judge(
            request,
            "watcher",
            read_at=request.watcher.last_tick,
            observed_at=_stamp(request.watcher.last_tick),
        )
    ]
    sources.append(
        _judge(
            request,
            "ledger",
            read_at=now,
            observed_at=_stamp(newest_row) if newest_row is not None else None,
        )
        if request.ledger
        else _unavailable("ledger", "no ledger rows were read")
    )
    sources.append(
        _judge(request, "linear", read_at=now)
        if request.linear
        else _unavailable("linear", "no Linear issues were read")
    )
    sources.append(
        _judge(request, "placement", read_at=now)
        if request.placement
        else _unavailable("placement", "no placement advertisement was read")
    )
    sources.append(_pool(request))
    for source_id, present, cause in (
        ("host_table", request.host_table is not None, "the host table was not read"),
        (
            "placement_markers",
            request.placed is not None,
            "the lane placement markers were not read",
        ),
        ("receipts", request.receipts is not None, "the lane receipts were not read"),
        (
            "cloud_accounts",
            request.cloud_accounts is not None,
            "the cloud accounts were not read",
        ),
        ("stoppage_report", bool(request.stoppages), "no friction report was read"),
    ):
        sources.append(
            _judge(request, source_id, read_at=now)
            if present
            else _unavailable(source_id, cause)
        )
    fleet = request.runner_fleet
    sources.append(
        _judge(
            request,
            "runner_fleet",
            read_at=fleet.observed_at,
            observed_at=_stamp(fleet.observed_at),
        )
        if fleet is not None
        else _unavailable(
            "runner_fleet",
            request.runner_fleet_cause or "the runner fleet was not read",
        )
    )
    count = {
        state: sum(1 for s in sources if s.state == state)
        for state in ("available", "partial", "overdue", "unavailable", "snapshot")
    }
    return ModelFloorDataHealth(
        generated_at=_stamp(now),
        published_at=None,
        overdue_after_min=request.config.data_health.overdue_after_min,
        stale_after_min=request.config.data_health.stale_after_min,
        sources=tuple(sources),
        summary=ModelFloorDataHealthSummary(
            **count, unread=sum(1 for s in sources if s.read_at is None)
        ),
    )


class HandlerFloorDataHealth:
    """Synchronous adapter for the data-health projection."""

    def handle(self, request: ModelFactoryFloorRequest) -> ModelFloorDataHealth:
        """Compute the data-health fact from the supplied immutable facts."""
        return data_health(request)
