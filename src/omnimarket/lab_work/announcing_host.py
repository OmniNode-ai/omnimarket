# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lab-work host that announces its facts beside each capacity advertisement (OMN-24367).

Kept apart from ``bus.py`` and ``cli.py`` so the baselined files do not grow. The announcement is new and read by
nothing yet, so a failed announcement is logged and never stops the capacity beat or the serve process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import click

from omnimarket.lab_work.bus import DEFAULT_TOOLS, LabWorkHost
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_introspection_announce_effect.handlers import (
    HostAnnouncer,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models import (
    ModelHostFactsProbeRequest,
)

logger = logging.getLogger(__name__)


def announce_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """The serve options that say what the host announces; the values reach ``AnnouncingLabWorkHost``."""
    for option in (
        click.option(
            "--announce/--no-announce",
            default=True,
            show_default=True,
            help="Announce this host's facts on the bus beside its capacity advertisement.",
        ),
        click.option(
            "--served-model",
            "served_models",
            multiple=True,
            help="A model this host serves, announced on the bus (repeat for several).",
        ),
        click.option(
            "--role",
            "roles",
            multiple=True,
            help="A role this host holds, announced on the bus (repeat for several).",
        ),
    ):
        command = option(command)
    return command


class AnnouncingLabWorkHost(LabWorkHost):
    """``LabWorkHost`` plus a ``HostAnnouncer`` called once per capacity beat and on the way out."""

    def __init__(
        self,
        *args: Any,
        roles: tuple[str, ...] = (),
        served_models: tuple[str, ...] = (),
        announce: bool = True,
        announcer: HostAnnouncer | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._announcer = announcer
        if announcer is None and announce:
            self._announcer = HostAnnouncer(
                self._bus,
                ModelHostFactsProbeRequest(
                    host_name=self._host,
                    roles=list(roles),
                    models_served=list(served_models),
                    software=list(DEFAULT_TOOLS),
                ),
            )
        self._last_ad: ModelHostCapacityAdvertisement | None = None

    async def advertise_once(self) -> ModelHostCapacityAdvertisement | None:
        ad = await super().advertise_once()
        if ad is not None:
            self._last_ad = ad
            await self._announce()
        return ad

    async def _announce(self, stopping: bool = False) -> None:
        if self._announcer is None or self._last_ad is None:
            return
        try:
            call = self._announcer.stop if stopping else self._announcer.announce
            await asyncio.wait_for(call(self._last_ad), self._topics.cadence_seconds)
        except Exception:
            logger.exception("lab-work host %s: host announcement failed", self._host)

    async def stop(self) -> None:
        if self._beat_task is not None:
            await self._announce(stopping=True)
        await super().stop()
