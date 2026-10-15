# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-work caller boundary: the shared LabWorkCaller places and sends units."""

from typing import Protocol

from omnimarket.lab_work.placement import ModelPlacement
from omnimarket.models.model_lab_work_unit import (
    ModelHostCapacityAdvertisement,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class ProtocolMorningLabWorkCaller(Protocol):
    async def start(self) -> None: ...
    async def collect(
        self, window_seconds: float
    ) -> list[ModelHostCapacityAdvertisement]: ...
    def place(
        self, *, need_tools: tuple[str, ...] = (), only_host: str | None = None
    ) -> ModelPlacement: ...
    async def dispatch(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt: ...
