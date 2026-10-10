# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan a lab-fill fire's headroom from the pool hosts' bus readings (OMN-20867).

The fire-plan route of node_lab_fill_plan_compute. node_lab_fill_orchestrator sends the
fire with the newest capacity advertisement of every host it has heard from; each
advertisement doubles as that host's probe, because the serve process states its own
unit slots in it (``max_units`` less ``running_units``) and its engine logins in
``tools``. The plan is HandlerLabFillHeadroomPlan's, keyed by the fire_id, so a stale
reading for a host with an idle slot is HEADROOM_UNKNOWN. A fire with no reading at all
knows no host's headroom and is HEADROOM_UNKNOWN too, never an empty success.
"""

from __future__ import annotations

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_lab_fill_fire_headroom import (
    ModelLabFillFireHeadroomRequest,
)

from ..models import (
    EnumLabFillPlanFailure,
    ModelLabFillHeadroomPlanRequest,
    ModelLabFillHeadroomPlanResult,
    ModelLabFillHostProbe,
)
from .handler_lab_fill_headroom_plan import HandlerLabFillHeadroomPlan

CLAUDE_LOGIN_TOOL = "claude-login"
CODEX_TOOL = "codex"


class HandlerLabFillFirePlan:
    """COMPUTE handler: a fire and its readings in, the fire's headroom plan out."""

    def __init__(self, headroom: HandlerLabFillHeadroomPlan | None = None) -> None:
        self._headroom = headroom or HandlerLabFillHeadroomPlan()

    def handle(
        self, request: ModelLabFillFireHeadroomRequest
    ) -> ModelLabFillHeadroomPlanResult:
        fire_id = request.fire.fire_id
        newest: dict[str, ModelHostCapacityAdvertisement] = {}
        for reading in request.readings:
            previous = newest.get(reading.host_name)
            if previous is None or reading.advertised_at > previous.advertised_at:
                newest[reading.host_name] = reading
        if not newest:
            return ModelLabFillHeadroomPlanResult(
                tick_id=fire_id,
                hosts=(),
                unknown=(),
                free=0,
                budget=0,
                terminal_failure_cause=EnumLabFillPlanFailure.HEADROOM_UNKNOWN,
            )
        probes = tuple(
            ModelLabFillHostProbe(
                name=name,
                runner_slots=max(0, reading.max_units - reading.running_units),
                login_claude=CLAUDE_LOGIN_TOOL in reading.tools,
                codex_ok=CODEX_TOOL in reading.tools,
            )
            for name, reading in sorted(newest.items())
        )
        return self._headroom.handle(
            ModelLabFillHeadroomPlanRequest(
                tick_id=fire_id,
                observed_at=request.observed_at,
                probes=probes,
                readings=tuple(newest.values()),
                max_lanes=request.max_lanes,
            )
        )


__all__: list[str] = ["HandlerLabFillFirePlan"]
