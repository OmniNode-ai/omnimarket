# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placement of a lab work unit from the pool's capacity advertisements
(OMN-20105).

Pure and deterministic: the advertisements and the evaluation time arrive as
data, nothing is read from a clock, a socket or a machine. Same input, same
decision.

Rules, in order, per host (the newest advertisement of each host counts):

1. **Staleness before headroom** (the OMN-14977 ordering). An advertisement
   older than ``2 * cadence_seconds``, or dated in the future beyond the same
   bound (a clock anomaly), is stale. A stale host is never placed on, however
   idle it last looked.
2. The host the caller named with ``only_host``, when it named one.
3. **The host table** (OMN-17232): the host is a row of it, the row is not
   ``available: false``, takes the unit's work kind and weight and has every
   capability the unit needs (:mod:`omnimarket.nodes.node_lab_placement_compute`). An
   advertisement alone admits nothing: a host the table refuses takes no unit
   however idle it is.
4. The tools the unit needs.
5. The load bar: load per core, counting the units the host is already
   running, at most ``max_load_per_core``. The launching Mac is a pool member
   like any other and passes or fails the same bar (operator ruling
   2026-09-29: work may run on it when its load is low enough).
6. Free memory at least ``min_free_bytes``, or the host row's own
   ``min_free_gb`` when it declares one (an old light-duty host).
7. A free unit slot (``running_units < max_units``).

The eligible host with the lowest load per core divided by its measured
``speed`` (host table, default from the rules), plus its advertised
``rank_penalty``, wins (the penalty ranks, it never bars); ties go to more free
memory, then the host name. When no host is eligible the decision is REFUSED
with a reason that keeps "could not check" apart from "over capacity": no
fresh advertisement at all is ``could_not_check``, fresh hosts that all fail a
bar is ``over_capacity`` (or ``missing_tools`` when tools were the only
reason, or ``host_table`` when the table refused every fresh host). The
reasons produce different operator instructions, so they are never conflated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_lab_placement_compute.models import ModelHostTableVerdict
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityAdvertisement,
)

DEFAULT_MAX_LOAD_PER_CORE = 0.75
DEFAULT_MIN_FREE_BYTES = 4 * 1024**3


class EnumPlacementDecision(StrEnum):
    PLACED = "placed"
    REFUSED = "refused"


class EnumRefusalReason(StrEnum):
    COULD_NOT_CHECK = "could_not_check"
    OVER_CAPACITY = "over_capacity"
    MISSING_TOOLS = "missing_tools"
    HOST_TABLE = "host_table"


class ModelHostVerdict(BaseModel):
    """Why one host was or was not eligible."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    eligible: bool
    reason: str
    load_per_core: float
    mem_available_gb: float
    age_seconds: float


class ModelPlacement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: EnumPlacementDecision
    host_name: str = ""
    refusal: EnumRefusalReason | None = None
    verdicts: list[ModelHostVerdict]

    def describe(self) -> str:
        lines = [
            f"placement: {self.decision.value}"
            + (f" on {self.host_name}" if self.host_name else "")
            + (f" ({self.refusal.value})" if self.refusal else "")
        ]
        lines.extend(
            f"  {v.host_name}: {'ELIGIBLE' if v.eligible else v.reason} "
            f"load/core={v.load_per_core:.2f} free={v.mem_available_gb:.1f}GB "
            f"age={v.age_seconds:.0f}s"
            for v in self.verdicts
        )
        return "\n".join(lines)


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


def place(
    advertisements: Sequence[ModelHostCapacityAdvertisement],
    *,
    evaluated_at: datetime,
    table_verdicts: Mapping[str, ModelHostTableVerdict],
    max_load_per_core: float = DEFAULT_MAX_LOAD_PER_CORE,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    need_tools: Iterable[str] = (),
    only_host: str | None = None,
) -> ModelPlacement:
    """Place one unit, or refuse with a typed reason."""
    need = frozenset(need_tools)
    verdicts: list[ModelHostVerdict] = []
    eligible: list[ModelHostCapacityAdvertisement] = []
    fresh_seen = False
    tools_refused = False
    capacity_refused = False
    for ad in newest_per_host(advertisements):
        age = (evaluated_at - ad.advertised_at).total_seconds()
        bound = timedelta(seconds=2 * ad.cadence_seconds).total_seconds()
        table = table_verdicts.get(ad.host_name)
        reason = ""
        if age > bound or age < -bound:
            reason = f"stale ({age:.0f}s old, bound {bound:.0f}s)"
        elif only_host is not None and ad.host_name != only_host:
            reason = "not the requested host"
        else:
            fresh_seen = True
            if table is None or not table.eligible:
                reason = "host table: " + (
                    table.reason if table else "the host table did not judge this host"
                )
            elif not need <= set(ad.tools):
                tools_refused = True
                reason = f"missing tools {sorted(need - set(ad.tools))}"
            else:
                bar = (
                    int(table.min_free_gb * 1024**3)
                    if table.min_free_gb is not None
                    else min_free_bytes
                )
                capacity_refused = True
                if ad.load_per_core > max_load_per_core:
                    reason = f"over the {max_load_per_core:.2f} load/core bar"
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
                ad.load_per_core / table_verdicts[ad.host_name].speed + ad.rank_penalty,
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


__all__ = [
    "DEFAULT_MAX_LOAD_PER_CORE",
    "DEFAULT_MIN_FREE_BYTES",
    "EnumPlacementDecision",
    "EnumRefusalReason",
    "ModelHostVerdict",
    "ModelPlacement",
    "newest_per_host",
    "place",
]
