# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Place a unit using the host table and current capacity advertisements."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from typing import Literal

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable

from ..models import (
    LAB_WORK_WEIGHT_ENV,
    EnumPlacementDecision,
    EnumRefusalReason,
    ModelHostVerdict,
    ModelLabPlacementRequest,
    ModelLabPoolPlaceRequest,
    ModelPlacement,
)
from .handler_lab_placement import HandlerLabPlacement, HostTableError, load_host_table


def newest_per_host(
    advertisements: Iterable[ModelHostCapacityAdvertisement],
) -> list[ModelHostCapacityAdvertisement]:
    """The newest advertisement of each host, ordered by host name."""
    newest: dict[str, ModelHostCapacityAdvertisement] = {}
    for ad in advertisements:
        held = newest.get(ad.host_name)
        if held is None or ad.advertised_at > held.advertised_at:
            newest[ad.host_name] = ad
    return [newest[name] for name in sorted(newest)]


class HandlerLabPoolPlace:
    """COMPUTE handler: table admission followed by capacity admission and ranking."""

    def __init__(
        self,
        *,
        table_source: Callable[[], ModelLabHostsTable] = load_host_table,
        judge: HandlerLabPlacement | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._table_source = table_source
        self._judge = judge
        self._environ = environ

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelLabPoolPlaceRequest) -> ModelPlacement:
        try:
            document = request.table or self._table_source()
            environ = os.environ if self._environ is None else self._environ
            weight = request.weight or environ.get(LAB_WORK_WEIGHT_ENV)
            judge = self._judge or HandlerLabPlacement()
            judged = judge.handle(
                ModelLabPlacementRequest(
                    repo=request.repo,
                    kind=request.kind,
                    weight=weight,
                    host_names=tuple(
                        sorted({ad.host_name for ad in request.advertisements})
                    ),
                    table=document.hosts,
                    unit_needs=document.unit_needs,
                )
            )
        except HostTableError as exc:
            return ModelPlacement(
                decision=EnumPlacementDecision.REFUSED,
                refusal=EnumRefusalReason.COULD_NOT_CHECK,
                verdicts=[],
                detail=str(exc),
            )
        table_verdicts = judged.by_host()
        need = frozenset(request.need_tools)
        verdicts: list[ModelHostVerdict] = []
        eligible: list[ModelHostCapacityAdvertisement] = []
        fresh_seen = False
        tools_refused = False
        capacity_refused = False
        for ad in newest_per_host(request.advertisements):
            age = (request.evaluated_at - ad.advertised_at).total_seconds()
            bound = timedelta(seconds=2 * ad.cadence_seconds).total_seconds()
            table = table_verdicts.get(ad.host_name)
            reason = ""
            if age > bound or age < -bound:
                reason = f"stale ({age:.0f}s old, bound {bound:.0f}s)"
            elif request.only_host is not None and ad.host_name != request.only_host:
                reason = "not the requested host"
            else:
                fresh_seen = True
                if table is None or not table.eligible:
                    reason = "host table: " + (
                        table.reason
                        if table
                        else "the host table did not judge this host"
                    )
                elif not need <= set(ad.tools):
                    tools_refused = True
                    reason = f"missing tools {sorted(need - set(ad.tools))}"
                else:
                    bar = (
                        int(table.min_free_gb * 1024**3)
                        if table.min_free_gb is not None
                        else request.min_free_bytes
                    )
                    capacity_refused = True
                    if ad.load_per_core > request.max_load_per_core:
                        reason = (
                            f"over the {request.max_load_per_core:.2f} load/core bar"
                        )
                    elif ad.mem_available_bytes < bar:
                        reason = "short of free memory" + (
                            f" (this host's own bar is {bar / 1024**3:.1f}GB)"
                            if table.min_free_gb is not None
                            else ""
                        )
                    elif ad.running_units >= ad.max_units:
                        reason = f"all {ad.max_units} unit slots busy"
            if not reason:
                eligible.append(ad)
            verdicts.append(
                ModelHostVerdict(
                    host_name=ad.host_name,
                    eligible=not reason,
                    reason=reason or "eligible",
                    load_per_core=round(ad.load_per_core, 3),
                    mem_available_gb=round(ad.mem_available_bytes / 1024**3, 1),
                    age_seconds=round(age, 1),
                )
            )
        if eligible:
            best = min(
                eligible,
                key=lambda ad: (
                    ad.load_per_core / table_verdicts[ad.host_name].speed
                    + ad.rank_penalty,
                    -ad.mem_available_bytes,
                    ad.host_name,
                ),
            )
            return ModelPlacement(
                decision=EnumPlacementDecision.PLACED,
                host_name=best.host_name,
                verdicts=verdicts,
            )
        if not fresh_seen:
            refusal = EnumRefusalReason.COULD_NOT_CHECK
        elif capacity_refused:
            refusal = EnumRefusalReason.OVER_CAPACITY
        elif tools_refused:
            refusal = EnumRefusalReason.MISSING_TOOLS
        else:
            refusal = EnumRefusalReason.HOST_TABLE
        return ModelPlacement(
            decision=EnumPlacementDecision.REFUSED, refusal=refusal, verdicts=verdicts
        )


__all__ = ["HandlerLabPoolPlace", "newest_per_host"]
