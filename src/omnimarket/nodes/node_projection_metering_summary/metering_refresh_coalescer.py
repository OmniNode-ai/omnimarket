# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Coalesce the metering summary refreshes (OMN-24492).

``node_metering_summary_refresh_compute`` emits one refresh command per
delegation terminal and one whole refresh per schedule window, and each makes
the writer refold every stored run of its tenants. This pure, clock-injected
coalescer lets the writer run at most one day-refresh per tenant per interval
and remembers the days it held back, which the next run carries.

``complete`` settles the days the finished run carried; ``rollback`` undoes a
failed run so the retry runs at once. No clock is read; ``now`` is passed in.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import date, datetime, timedelta

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_metering_summary_refresh_requested import (
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_projection_metering_summary.models.model_metering_refresh_coalesce import (
    EnumMeteringRefreshVerdict,
    ModelMeteringRefreshCoalesceDecision,
    ModelMeteringRefreshCoalesceOverlay,
    ModelMeteringRefreshCoalesceStats,
)


class MeteringRefreshCoalescer:
    """At most one day-refresh per tenant per interval; the rest is held back.

    A whole refresh always runs and settles every tenant's pending days; a
    named full refold always runs and settles its tenants' pending days. A
    named day-refresh runs only when the tenant's last run, or the last whole
    refresh, is at the interval old; otherwise its days join the tenant's
    pending set and the next run carries them.
    """

    def __init__(self, min_interval: timedelta) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_run: dict[str, datetime] = {}
        self._whole_at: datetime | None = None
        self._pending: dict[str, set[date]] = {}
        self._runs = 0
        self._coalesced = 0

    @property
    def min_interval(self) -> timedelta:
        """The minimum spacing between two day refreshes of one tenant."""
        return self._min_interval

    def admit(
        self,
        request: ModelMeteringSummaryRefreshRequested,
        now: datetime,
    ) -> ModelMeteringRefreshCoalesceDecision:
        """Decide, at ``now``, whether the writer runs ``request`` or holds it back."""
        with self._lock:
            if self._min_interval == timedelta(0):
                self._runs += 1
                return ModelMeteringRefreshCoalesceDecision(
                    verdict=EnumMeteringRefreshVerdict.RUN,
                    request=request,
                    admitted_at=now,
                )

            if request.tenant_ids is None:
                settles = self._settles_for(sorted(self._pending))
                self._whole_at = now
                self._runs += 1
                return ModelMeteringRefreshCoalesceDecision(
                    verdict=EnumMeteringRefreshVerdict.RUN,
                    request=request,
                    admitted_at=now,
                    settles=settles,
                )

            if request.days is None:
                settles = self._settles_for(request.tenant_ids)
                for tenant in request.tenant_ids:
                    self._last_run[tenant] = now
                self._runs += 1
                return ModelMeteringRefreshCoalesceDecision(
                    verdict=EnumMeteringRefreshVerdict.RUN,
                    request=request,
                    admitted_at=now,
                    settles=settles,
                    tenants=request.tenant_ids,
                )

            held: list[str] = []
            due: list[str] = []
            for tenant in sorted(request.tenant_ids):
                last = self._last_of(tenant)
                if last is not None and timedelta(0) <= now - last < self._min_interval:
                    held.append(tenant)
                    self._pending.setdefault(tenant, set()).update(request.days)
                    self._coalesced += 1
                else:
                    due.append(tenant)

            if not due:
                return ModelMeteringRefreshCoalesceDecision(
                    verdict=EnumMeteringRefreshVerdict.COALESCED,
                    request=None,
                    admitted_at=now,
                )

            days = request.days
            due_settles: list[tuple[str, frozenset[date]]] = []
            for tenant in due:
                pending_days: frozenset[date] = frozenset(self._pending.get(tenant, ()))
                if pending_days:
                    days = days | pending_days
                    due_settles.append((tenant, pending_days))
            for tenant in due:
                self._last_run[tenant] = now
            self._runs += 1
            return ModelMeteringRefreshCoalesceDecision(
                verdict=EnumMeteringRefreshVerdict.RUN,
                request=request.model_copy(
                    update={"tenant_ids": tuple(due), "days": days}
                ),
                admitted_at=now,
                settles=tuple(due_settles),
                tenants=tuple(due),
            )

    def complete(self, decision: ModelMeteringRefreshCoalesceDecision) -> None:
        """The run finished: settle the days it carried, keeping the rest held."""
        with self._lock:
            for tenant, days in decision.settles:
                remaining = self._pending.get(tenant, set()) - set(days)
                if remaining:
                    self._pending[tenant] = remaining
                else:
                    self._pending.pop(tenant, None)

    def rollback(self, decision: ModelMeteringRefreshCoalesceDecision) -> None:
        """The run failed: undo this admission so the retry runs at once."""
        with self._lock:
            for tenant in decision.tenants:
                if self._last_run.get(tenant) == decision.admitted_at:
                    del self._last_run[tenant]
            if decision.verdict is EnumMeteringRefreshVerdict.RUN and self._runs > 0:
                self._runs -= 1

    def stats(self) -> ModelMeteringRefreshCoalesceStats:
        """A snapshot copy of the counts and the tenants with held-back days."""
        with self._lock:
            return ModelMeteringRefreshCoalesceStats(
                runs=self._runs,
                coalesced=self._coalesced,
                pending={
                    tenant: frozenset(days)
                    for tenant, days in self._pending.items()
                    if days
                },
            )

    def _settles_for(
        self, tenants: tuple[str, ...] | list[str]
    ) -> tuple[tuple[str, frozenset[date]], ...]:
        """The pending days of the named tenants that actually hold days back."""
        return tuple(
            (tenant, frozenset(self._pending[tenant]))
            for tenant in tenants
            if self._pending.get(tenant)
        )

    def _last_of(self, tenant: str) -> datetime | None:
        """The later of the tenant's last day refresh and the last whole refresh."""
        stamps = [
            stamp
            for stamp in (self._last_run.get(tenant), self._whole_at)
            if stamp is not None
        ]
        return max(stamps) if stamps else None


def coalesce_interval_from_overlay(
    default_seconds: int,
    environ: Mapping[str, str] | None = None,
) -> timedelta:
    """The coalescer's interval from the node's overlay, else the default.

    A missing overlay reads as the default; a broken one raises, so a broken
    overlay is never read as an absent one.
    """
    try:
        overlay = load_structured_overlay(
            "node_projection_metering_summary",
            ModelMeteringRefreshCoalesceOverlay,
            environ,
        )
    except NodeOverlayNotFoundError:
        return timedelta(seconds=default_seconds)
    return timedelta(seconds=overlay.min_interval_seconds)


__all__: list[str] = [
    "MeteringRefreshCoalescer",
    "coalesce_interval_from_overlay",
]
