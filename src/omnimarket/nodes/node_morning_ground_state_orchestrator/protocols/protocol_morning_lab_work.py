# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-work caller boundary: the shared LabWorkCaller places and sends units."""

from typing import Protocol

from omnimarket.models.model_lab_work_unit import (
    ModelHostCapacityAdvertisement,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class ProtocolMorningPlacement(Protocol):
    """What the morning phase reads from a placement decision."""

    @property
    def placed(self) -> bool: ...
    @property
    def host_name(self) -> str: ...
    def describe(self) -> str: ...


class ProtocolMorningLabWorkCaller(Protocol):
    async def start(self) -> None: ...
    async def collect(
        self, window_seconds: float
    ) -> list[ModelHostCapacityAdvertisement]: ...
    def place(
        self,
        *,
        need_tools: tuple[str, ...] = (),
        only_host: str | None = None,
        repo: str = "",
    ) -> ProtocolMorningPlacement: ...
    async def dispatch(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt: ...
