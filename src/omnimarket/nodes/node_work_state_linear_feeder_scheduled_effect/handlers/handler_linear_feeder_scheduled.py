# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The linear feeder of the work-state projection on the runtime's schedule (OMN-23427).

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
from omnimarket.nodes.node_work_state_linear_feeder_scheduled_effect.protocols.adapter_linear_feeder import (
    LinearSnapshotFeeder,
)
from omnimarket.protocols.protocol_work_state_feeder import ProtocolWorkStateFeeder

SCHEDULE_OVERLAY_ID = "node_work_state_linear_feeder_scheduled_effect"
SCHEDULE_OVERLAY_FILE_ENV = "ONEX_WORK_STATE_LINEAR_FEEDER_OVERLAY"


def live_feeder(environ: Mapping[str, str] | None) -> ProtocolWorkStateFeeder:
    """The live profile's feeder, built from the environment's overlays."""
    return LinearSnapshotFeeder.from_environ(environ)


class HandlerWorkStateLinearFeederScheduled(HandlerWorkStateFeederScheduled):
    """Gate on the schedule, feed the linear source, return the feeder's event."""

    def __init__(self, **bound: Any) -> None:
        super().__init__(
            feeder=EnumFeeder.LINEAR,
            overlay_id=SCHEDULE_OVERLAY_ID,
            overlay_file_env=SCHEDULE_OVERLAY_FILE_ENV,
            live=live_feeder,
            **bound,
        )
