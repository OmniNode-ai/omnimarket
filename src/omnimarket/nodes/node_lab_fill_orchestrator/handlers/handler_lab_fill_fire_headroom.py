# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabFillFireHeadroom: a lab-fill fire becomes one headroom plan request (OMN-20867).

The fire route of node_lab_fill_orchestrator. node_lab_fill_plan_compute publishes a
lab-fill fire (ModelScheduledFire) on its fired topic when the runtime tick opens a
lab-fill window, beside the plans; every record there reaches this route and only a fire
is acted on. This handler takes the newest capacity reading of every pool host the node
holds, stamps the assembly time and emits one ModelLabFillFireHeadroomRequest, as an
envelope naming the compute's fire-plan topic, whose result leaves on the decided or the
failure terminal.

The tick topic republishes a tick, so the same fire arrives more than once: a fire_id
already planned emits nothing. The bounded memory answers within one process; the
request's correlation id is derived from the fire_id, so a copy emitted after a restart
names the same fire. Fires of other workflows are ignored. Effect work is not dispatched
here.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.model_lab_fill_fire_headroom import (
    ModelLabFillDecidedRecord,
    ModelLabFillFireHeadroomRequest,
)

from ..models import ModelLabFillOrchestratorConfig
from .contract_config import fire_plan_topic, orchestrator_config
from .handler_lab_fill_host_readings import LAB_HOST_READINGS, LabHostReadings


def fire_correlation_id(fire_id: str) -> uuid.UUID:
    """The same fire always names the same correlation id."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"omnimarket:lab-fill-fire:{fire_id}")


class HandlerLabFillFireHeadroom:
    """ORCHESTRATOR handler: a lab-fill fire in, its headroom plan request out, once."""

    def __init__(
        self,
        readings: LabHostReadings | None = None,
        config: ModelLabFillOrchestratorConfig | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._readings = LAB_HOST_READINGS if readings is None else readings
        self._cfg = config or orchestrator_config()
        self._topic = fire_plan_topic()
        self._now = now
        self._planned: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def _first_sight(self, fire_id: str) -> bool:
        with self._lock:
            if fire_id in self._planned:
                self._planned.move_to_end(fire_id)
                return False
            self._planned[fire_id] = None
            while len(self._planned) > self._cfg.max_fires_remembered:
                self._planned.popitem(last=False)
            return True

    def handle(self, request: ModelLabFillDecidedRecord) -> ModelHandlerOutput[None]:
        fire = request.as_fire()
        key = fire.fire_id if fire is not None else request.model_dump_json()
        correlation_id = fire_correlation_id(key)
        events: tuple[ModelEventEnvelope[object], ...] = ()
        if (
            fire is not None
            and fire.workflow == self._cfg.workflow
            and self._first_sight(fire.fire_id)
        ):
            plan_request = ModelLabFillFireHeadroomRequest(
                fire=fire,
                observed_at=self._now(),
                readings=self._readings.snapshot(),
                max_lanes=self._cfg.max_lanes,
            )
            envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
                payload=plan_request,
                correlation_id=correlation_id,
                event_type=self._topic,
            )
            events = (envelope,)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_lab_fill_orchestrator.plan_lab_fill_fire",
            events=events,
        )


__all__: list[str] = ["HandlerLabFillFireHeadroom", "fire_correlation_id"]
