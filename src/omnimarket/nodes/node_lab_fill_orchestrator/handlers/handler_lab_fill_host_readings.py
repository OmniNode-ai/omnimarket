# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabFillHostReadings: hold the newest capacity reading of every pool host (OMN-20867).

The capacity route of node_lab_fill_orchestrator. Each lab-work serve process publishes
its host's capacity advertisement on the contract's readings topic every cadence; this
handler keeps the newest one per host and emits nothing. The fire route assembles a
fire's headroom request from what is held. The runtime builds one handler instance per
route, so both routes share the node's process-wide ``LAB_HOST_READINGS``; a restart
starts it empty and the next cadence refills it.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

from .contract_config import orchestrator_config


class LabHostReadings:
    """The newest reading per host, bounded to the contract's ``max_hosts``."""

    def __init__(self, max_hosts: int | None = None) -> None:
        self._max_hosts = (
            orchestrator_config().max_hosts if max_hosts is None else max_hosts
        )
        self._newest: OrderedDict[str, ModelHostCapacityAdvertisement] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, reading: ModelHostCapacityAdvertisement) -> bool:
        """Keep ``reading`` when it is the host's newest; True when it was kept."""
        with self._lock:
            previous = self._newest.get(reading.host_name)
            if previous is not None and reading.advertised_at <= previous.advertised_at:
                return False
            self._newest[reading.host_name] = reading
            self._newest.move_to_end(reading.host_name)
            while len(self._newest) > self._max_hosts:
                self._newest.popitem(last=False)
            return True

    def snapshot(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        with self._lock:
            return tuple(self._newest[name] for name in sorted(self._newest))

    def clear(self) -> None:
        with self._lock:
            self._newest.clear()


LAB_HOST_READINGS = LabHostReadings()


class HandlerLabFillHostReadings:
    """ORCHESTRATOR handler: a capacity reading in, held; nothing emitted."""

    def __init__(self, readings: LabHostReadings | None = None) -> None:
        self._readings = LAB_HOST_READINGS if readings is None else readings

    def handle(
        self, request: ModelHostCapacityAdvertisement
    ) -> ModelHandlerOutput[None]:
        self._readings.record(request)
        reading_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnimarket:lab-host-capacity:{request.host_name}:"
            f"{request.advertised_at.isoformat()}",
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=reading_id,
            correlation_id=reading_id,
            handler_id="node_lab_fill_orchestrator.record_lab_host_capacity",
        )


__all__: list[str] = [
    "LAB_HOST_READINGS",
    "HandlerLabFillHostReadings",
    "LabHostReadings",
]
