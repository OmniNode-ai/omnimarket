"""Handler protocols a lab work host serves."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class ProtocolWorkHandler(Protocol):
    @property
    def host_name(self) -> str:
        """The name the host advertises itself under."""

    async def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        """The receipt of the unit this host served."""


class ProtocolAdvertiseHandler(Protocol):
    def handle(
        self, request: ModelHostCapacityProbeRequest
    ) -> ModelHostCapacityAdvertisement:
        """The host's capacity advertisement for the probe."""
