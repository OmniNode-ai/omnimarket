# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure additive profile latency qualification for plan task TC.12 (OMN-21134).

The caller drives the recorded round trips (20 cold and 20 warm per additive
profile and host) through the adapter and passes each one's duration. This node
groups them by profile, host and temperature, reports p50, p95 and max (nearest
rank), and for each profile proposes the ``latency_budget_ms`` its profile file
should carry: the worst p95 of the hosts that can carry the row, rounded up, never
above that profile's decision-D8 cap. A host whose p95 exceeds the cap is not
promotable there and names the warming remedy (2026-09-19 plan, section 10). A
missing profile, host or short cell withholds every proposal, and a caller
candidate below the measured p95 or above the cap is listed as a falsifier. It
performs no I/O and measures nothing itself.
"""

from __future__ import annotations

import math
from collections import defaultdict

from omnimarket.models.claude_code_binding import ADDITIVE_BUDGET_CAPS_MS
from omnimarket.nodes.node_context_additive_latency_compute.models import (
    ModelAdditiveHostVerdict,
    ModelAdditiveLatencyCell,
    ModelAdditiveLatencyRequest,
    ModelAdditiveLatencyResult,
    ModelAdditiveLatencyStats,
    ModelAdditiveProfileProposal,
    ModelAdditiveRoundTrip,
)

__all__ = ["HandlerAdditiveLatency"]

WARMING_REMEDY = (
    "p95 exceeds the cap on this host: not promotable here; plan the warming of "
    "the 2026-09-19 system-level learning plan, section 10"
)


def _nearest_rank(sorted_values: list[float], percent: int) -> float:
    return sorted_values[math.ceil(percent / 100 * len(sorted_values)) - 1]


def _stats(values: list[float]) -> ModelAdditiveLatencyStats:
    ordered = sorted(values)
    return ModelAdditiveLatencyStats(
        p50_ms=_nearest_rank(ordered, 50),
        p95_ms=_nearest_rank(ordered, 95),
        max_ms=ordered[-1],
    )


class HandlerAdditiveLatency:
    """handle(ModelAdditiveLatencyRequest) -> ModelAdditiveLatencyResult."""

    def handle(
        self, request: ModelAdditiveLatencyRequest
    ) -> ModelAdditiveLatencyResult:
        groups: dict[tuple[str, str, str], list[ModelAdditiveRoundTrip]] = defaultdict(
            list
        )
        for trip in request.round_trips:
            groups[(trip.profile, trip.host, trip.temperature.value)].append(trip)

        cells = tuple(
            ModelAdditiveLatencyCell(
                profile=profile,
                host=host,
                temperature=trips[0].temperature,
                count=len(trips),
                latency=_stats([t.duration_ms for t in trips]),
            )
            for (profile, host, _), trips in sorted(groups.items())
        )

        missing = self._missing(request, groups)
        complete = not missing
        proposals = tuple(
            self._proposal(profile, request, cells, complete=complete)
            for profile in request.required_profiles
        )
        return ModelAdditiveLatencyResult(
            cells=cells,
            complete=complete,
            proposals=proposals,
            falsifiers=(*missing, *self._candidates(request, proposals, complete)),
        )

    @staticmethod
    def _missing(
        request: ModelAdditiveLatencyRequest,
        groups: dict[tuple[str, str, str], list[ModelAdditiveRoundTrip]],
    ) -> tuple[str, ...]:
        seen_profiles = {key[0] for key in groups}
        seen_hosts = {key[1] for key in groups}
        absent_profiles = [
            p for p in request.required_profiles if p not in seen_profiles
        ]
        absent_hosts = [h for h in request.required_hosts if h not in seen_hosts]
        out = [f"profile {p} missing" for p in absent_profiles]
        out.extend(f"host {h} missing" for h in absent_hosts)
        for profile in request.required_profiles:
            for host in request.required_hosts:
                if profile in absent_profiles or host in absent_hosts:
                    continue
                for temperature in ("cold", "warm"):
                    have = len(groups.get((profile, host, temperature), ()))
                    if have < request.min_samples_per_cell:
                        out.append(
                            f"cell {profile}/{host}/{temperature}: {have} of "
                            f"{request.min_samples_per_cell} round trips"
                        )
        return tuple(out)

    @staticmethod
    def _proposal(
        profile: str,
        request: ModelAdditiveLatencyRequest,
        cells: tuple[ModelAdditiveLatencyCell, ...],
        *,
        complete: bool,
    ) -> ModelAdditiveProfileProposal:
        cap = ADDITIVE_BUDGET_CAPS_MS[profile]
        if not complete:
            return ModelAdditiveProfileProposal(
                profile=profile,
                cap_ms=cap,
                worst_p95_ms=None,
                proposed_budget_ms=None,
                hosts=tuple(
                    ModelAdditiveHostVerdict(
                        host=host, p95_ms=None, promotable=False, remedy=""
                    )
                    for host in request.required_hosts
                ),
            )
        verdicts: list[ModelAdditiveHostVerdict] = []
        for host in request.required_hosts:
            p95 = max(
                c.latency.p95_ms
                for c in cells
                if c.profile == profile and c.host == host
            )
            promotable = p95 <= cap
            verdicts.append(
                ModelAdditiveHostVerdict(
                    host=host,
                    p95_ms=p95,
                    promotable=promotable,
                    remedy="" if promotable else WARMING_REMEDY,
                )
            )
        carrying = [v.p95_ms for v in verdicts if v.promotable and v.p95_ms is not None]
        worst = max(carrying) if carrying else None
        return ModelAdditiveProfileProposal(
            profile=profile,
            cap_ms=cap,
            worst_p95_ms=worst,
            proposed_budget_ms=math.ceil(worst) if worst is not None else None,
            hosts=tuple(verdicts),
        )

    @staticmethod
    def _candidates(
        request: ModelAdditiveLatencyRequest,
        proposals: tuple[ModelAdditiveProfileProposal, ...],
        complete: bool,
    ) -> tuple[str, ...]:
        by_profile = {p.profile: p for p in proposals}
        out: list[str] = []
        for profile, budget in sorted(request.candidate_budgets_ms.items()):
            if not complete:
                out.append(f"candidate budget {profile} unchecked: table incomplete")
                continue
            proposal = by_profile[profile]
            if budget > proposal.cap_ms:
                out.append(
                    f"candidate budget {profile} {budget} ms is above the cap "
                    f"{proposal.cap_ms} ms"
                )
            elif proposal.worst_p95_ms is None:
                out.append(
                    f"candidate budget {profile} {budget} ms: no host can carry "
                    "the row within its cap"
                )
            elif budget < proposal.worst_p95_ms:
                out.append(
                    f"candidate budget {profile} {budget} ms is below the "
                    f"measured p95 {proposal.worst_p95_ms:g} ms"
                )
        return tuple(out)
