# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool's free hosts for one leg, through the reused placement compute (OMN-23627).

Exemplar: ``nodes/node_lab_fill_orchestrator/handlers/handler_lab_fill_host_readings.py``
(``LabHostReadings``: the newest advertisement per host) and
``nodes/node_lab_placement_compute`` (``HandlerLabPoolPlace``: table admission, capacity
admission, ranking). No placement of its own: a host ``place`` chooses leaves the leg's
snapshot, and the loop ends when ``place`` refuses.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_lab_fill_orchestrator.handlers.handler_lab_fill_host_readings import (
    LabHostReadings,
)
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_pool_place import (
    HandlerLabPoolPlace,
)
from omnimarket.nodes.node_lab_placement_compute.models.model_lab_pool_place import (
    EnumPlacementDecision,
    ModelLabPoolPlaceRequest,
)


class PlacedFreeHosts:
    """Free hosts from the held readings, chosen one at a time by ``place``."""

    def __init__(
        self,
        readings: LabHostReadings,
        *,
        place: HandlerLabPoolPlace | None = None,
        repo: str = "",
    ) -> None:
        self._readings = readings
        self._place = place if place is not None else HandlerLabPoolPlace()
        self._repo = repo

    def record(self, advertisement: object) -> None:
        if isinstance(advertisement, ModelHostCapacityAdvertisement):
            self._readings.record(advertisement)

    def free_hosts(self, now: datetime, need_tools: Sequence[str]) -> tuple[str, ...]:
        remaining = list(self._readings.snapshot())
        chosen: list[str] = []
        while remaining:
            placement = self._place.handle(
                ModelLabPoolPlaceRequest(
                    advertisements=tuple(remaining),
                    evaluated_at=now,
                    repo=self._repo,
                    kind="code_draft",
                    need_tools=tuple(need_tools),
                )
            )
            if (
                placement.decision is not EnumPlacementDecision.PLACED
                or not placement.host_name
            ):
                break
            chosen.append(placement.host_name)
            remaining = [ad for ad in remaining if ad.host_name != placement.host_name]
        return tuple(chosen)


class StaticFreeHosts:
    """A fixed host list, for tests and the in-memory profile."""

    def __init__(self, hosts: Sequence[str]) -> None:
        self._hosts = tuple(hosts)

    def record(self, advertisement: object) -> None:
        return None

    def free_hosts(self, now: datetime, need_tools: Sequence[str]) -> tuple[str, ...]:
        return self._hosts


__all__ = ["PlacedFreeHosts", "StaticFreeHosts"]
