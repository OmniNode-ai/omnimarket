# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readings feeder of the work-state projection on the runtime's schedule (OMN-23427).

The gate, the stale judgement and the event are the shared handler's; this class binds the feeder enum, the overlay and the
live adapter. Nothing here names a cadence, a path or a host.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.handlers.work_state_feeder_scheduled import (
    HandlerWorkStateFeederScheduled,
)
from omnimarket.models.work_state_feeder import EnumFeeder
from omnimarket.nodes.node_work_state_readings_feeder_scheduled_effect.protocols.adapter_readings_feeder import (
    ReadingsFeeder,
)
from omnimarket.protocols.protocol_work_state_feeder import ProtocolWorkStateFeeder

SCHEDULE_OVERLAY_ID = "node_work_state_readings_feeder_scheduled_effect"
SCHEDULE_OVERLAY_FILE_ENV = "ONEX_WORK_STATE_READINGS_FEEDER_OVERLAY"


def live_feeder(environ: Mapping[str, str] | None) -> ProtocolWorkStateFeeder:
    """The live profile's feeder, built from the environment's overlays."""
    return ReadingsFeeder.from_environ(environ)


class HandlerWorkStateReadingsFeederScheduled(HandlerWorkStateFeederScheduled):
    """Gate on the schedule, feed the readings source, return the feeder's event."""

    def __init__(self, **bound: Any) -> None:
        super().__init__(
            feeder=EnumFeeder.READINGS,
            overlay_id=SCHEDULE_OVERLAY_ID,
            overlay_file_env=SCHEDULE_OVERLAY_FILE_ENV,
            live=live_feeder,
            **bound,
        )
