# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan lab-fill lanes from pool hosts' capacity advertisements (OMN-20867)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

from ..models import (
    EnumLabFillPlanFailure,
    ModelLabFillHeadroomPlanRequest,
    ModelLabFillHeadroomPlanResult,
    ModelLabFillHeadroomUnknown,
    ModelLabFillHostCapacity,
)
from .handler_lab_fill_capacity import host_capacity


@lru_cache(maxsize=1)
def headroom_max_age_seconds() -> int:
    """Read the headroom freshness bound once from the packaged contract."""
    data = yaml.safe_load(
        (Path(__file__).parents[1] / "contract.yaml").read_text(encoding="utf-8")
    )
    return int(data["config"]["lab_fill_plan"]["headroom"]["max_age_seconds"])


class HandlerLabFillHeadroomPlan:
    """Plan lanes from fresh headroom and fail loudly for idle hosts without it.

    Each probe is the runner's placement read of a host (its slots, limited marker,
    refusal); its headroom is the newest capacity advertisement that host published
    on the bus. A host the runner blocks keeps the capacity handler's reason. A host
    with idle slots whose advertisement is missing, older than the contract's max
    age or dated past it is HEADROOM_UNKNOWN: no lanes, and the result carries the
    failure cause so the runtime publishes it on the failure terminal.
    """

    def __init__(self, max_age_seconds: int | None = None) -> None:
        self.max_age_seconds = (
            headroom_max_age_seconds() if max_age_seconds is None else max_age_seconds
        )

    def handle(
        self, request: ModelLabFillHeadroomPlanRequest
    ) -> ModelLabFillHeadroomPlanResult:
        newest: dict[str, ModelHostCapacityAdvertisement] = {}
        for reading in request.readings:
            previous = newest.get(reading.host_name)
            if previous is None or reading.advertised_at > previous.advertised_at:
                newest[reading.host_name] = reading

        hosts: list[ModelLabFillHostCapacity] = []
        unknown: list[ModelLabFillHeadroomUnknown] = []
        max_age = self.max_age_seconds
        for probe in request.probes:
            if probe.local:
                continue
            advertisement = newest.get(probe.name)
            age = (
                (request.observed_at - advertisement.advertised_at).total_seconds()
                if advertisement is not None
                else None
            )
            fresh = age is not None and -max_age <= age <= max_age
            status: Literal["missing", "stale", "future"] = "missing"
            if age is not None:
                status = "stale" if age > max_age else "future"
            facts: dict[str, object] = {
                "name": probe.name,
                "runner_slots": probe.runner_slots,
                "error": probe.error,
                "limited": probe.limited,
                "refusal": probe.refusal,
                "login_claude": probe.login_claude,
                "codex_ok": probe.codex_ok,
                "running_lanes": list(probe.running_lanes),
            }
            if fresh and advertisement is not None:
                facts.update(
                    cores=advertisement.cores,
                    load1=advertisement.load1,
                    mem_avail_gb=advertisement.mem_available_bytes / 1024**3,
                )
            if probe.error or probe.limited or probe.refusal:
                host = host_capacity(facts, request.policy)
            elif not fresh:
                reason = "runner-cap"
                if probe.runner_slots > 0:
                    reason = f"headroom-unknown: {status}"
                    unknown.append(
                        ModelLabFillHeadroomUnknown(
                            host=probe.name,
                            reason=status,
                            runner_slots=probe.runner_slots,
                            reading_age_seconds=age,
                            max_age_seconds=max_age,
                        )
                    )
                host = ModelLabFillHostCapacity(
                    name=probe.name,
                    lanes=0,
                    idle_lanes=0,
                    runner_slots=probe.runner_slots,
                    cap_bound=False,
                    codex=probe.codex_ok,
                    claude=probe.login_claude,
                    running=len(probe.running_lanes),
                    load_per_core=None,
                    reason=reason,
                )
            else:
                host = host_capacity(facts, request.policy)
            hosts.append(host)

        free = sum(host.lanes for host in hosts)
        return ModelLabFillHeadroomPlanResult(
            tick_id=request.tick_id,
            hosts=tuple(hosts),
            unknown=tuple(unknown),
            free=free,
            budget=min(request.max_lanes, free),
            terminal_failure_cause=(
                EnumLabFillPlanFailure.HEADROOM_UNKNOWN if unknown else None
            ),
        )
