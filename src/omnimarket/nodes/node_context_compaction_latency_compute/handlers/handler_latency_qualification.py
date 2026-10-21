# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compaction build latency qualification for plan task TA.12 (OMN-21114).

The caller drives the recorded round trips (20 cold and 20 warm per session size and
host) through the adapter and passes each one's two phase timings. This node groups
them by size, host and temperature, reports p50, p95 and max (nearest rank) for the
deterministic build, the model-written section and their per-round-trip total, and
proposes the context_compaction class ceiling (worst total p95 plus margin) and the
precompute deadline (worst total p95 plus slack). A missing size, host or short cell
withholds both proposals, and a caller-supplied candidate below the measured p95 is
listed as a falsifier. It performs no I/O and measures nothing itself.
"""

from __future__ import annotations

import math
from collections import defaultdict

from omnimarket.nodes.node_context_compaction_latency_compute.models import (
    ModelCompactionRoundTrip,
    ModelLatencyCell,
    ModelLatencyQualificationRequest,
    ModelLatencyQualificationResult,
    ModelLatencyStats,
)

__all__ = ["HandlerLatencyQualification"]


def _nearest_rank(sorted_values: list[float], percent: int) -> float:
    return sorted_values[math.ceil(percent / 100 * len(sorted_values)) - 1]


def _stats(values: list[float]) -> ModelLatencyStats:
    ordered = sorted(values)
    return ModelLatencyStats(
        p50_ms=_nearest_rank(ordered, 50),
        p95_ms=_nearest_rank(ordered, 95),
        max_ms=ordered[-1],
    )


class HandlerLatencyQualification:
    """handle(ModelLatencyQualificationRequest) -> ModelLatencyQualificationResult."""

    def handle(
        self, request: ModelLatencyQualificationRequest
    ) -> ModelLatencyQualificationResult:
        groups: dict[tuple[str, str, str], list[ModelCompactionRoundTrip]] = (
            defaultdict(list)
        )
        for trip in request.round_trips:
            groups[(trip.size, trip.host, trip.temperature.value)].append(trip)

        cells = tuple(
            ModelLatencyCell(
                size=size,
                host=host,
                temperature=trips[0].temperature,
                count=len(trips),
                deterministic_build=_stats([t.deterministic_build_ms for t in trips]),
                model_section=_stats([t.model_section_ms for t in trips]),
                total=_stats(
                    [t.deterministic_build_ms + t.model_section_ms for t in trips]
                ),
            )
            for (size, host, _), trips in sorted(groups.items())
        )

        falsifiers = self._missing(request, groups)
        complete = not falsifiers
        build_p95 = max((c.total.p95_ms for c in cells), default=None)
        ceiling: int | None = None
        deadline: int | None = None
        if complete and build_p95 is not None:
            ceiling = math.ceil(build_p95) + request.margin_ms
            deadline = math.ceil(build_p95) + request.precompute_slack_ms
        falsifiers = (
            *falsifiers,
            *self._candidates(request, complete=complete, build_p95=build_p95),
        )
        return ModelLatencyQualificationResult(
            cells=cells,
            complete=complete,
            build_p95_ms=build_p95 if complete else None,
            class_ceiling_ms=ceiling,
            precompute_deadline_ms=deadline,
            falsifiers=falsifiers,
        )

    @staticmethod
    def _missing(
        request: ModelLatencyQualificationRequest,
        groups: dict[tuple[str, str, str], list[ModelCompactionRoundTrip]],
    ) -> tuple[str, ...]:
        seen_sizes = {key[0] for key in groups}
        seen_hosts = {key[1] for key in groups}
        out: list[str] = []
        absent_sizes = [s for s in request.required_sizes if s not in seen_sizes]
        absent_hosts = [h for h in request.required_hosts if h not in seen_hosts]
        out.extend(f"size {s} missing" for s in absent_sizes)
        out.extend(f"host {h} missing" for h in absent_hosts)
        for size in request.required_sizes:
            for host in request.required_hosts:
                if size in absent_sizes or host in absent_hosts:
                    continue
                for temperature in ("cold", "warm"):
                    have = len(groups.get((size, host, temperature), ()))
                    if have < request.min_samples_per_cell:
                        out.append(
                            f"cell {size}/{host}/{temperature}: {have} of "
                            f"{request.min_samples_per_cell} round trips"
                        )
        return tuple(out)

    @staticmethod
    def _candidates(
        request: ModelLatencyQualificationRequest,
        *,
        complete: bool,
        build_p95: float | None,
    ) -> tuple[str, ...]:
        out: list[str] = []
        for label, value in (
            ("class ceiling", request.candidate_class_ceiling_ms),
            ("precompute deadline", request.candidate_precompute_deadline_ms),
        ):
            if value is None:
                continue
            if not complete or build_p95 is None:
                out.append(f"candidate {label} unchecked: table incomplete")
            elif value < build_p95:
                out.append(
                    f"candidate {label} {value} ms is below the measured p95 "
                    f"{build_p95:g} ms"
                )
        return tuple(out)
