# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the lab alarm's broker-lag check or container restart probe.

The scheduled job node reaches the lab alarm's broker-lag handler through this shared module, so no node imports
another node's models package. The alarm overlay is the lab alarm's own overlay (``ONEX_LAB_ALARM_OVERLAY`` pins the
file, else ``<alarm_overlay_id>/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``). An unreadable overlay raises
``NodeOverlayError``; the scheduled job node turns it into a failed run event.

The lab alarm's lag source arrives as an injected ``handler``, or as a ``build_handler`` callable that is handed the
loaded overlay; a runtime that binds a lag source passes one of the two. A runtime that binds no lag source passes
neither, and the tick reports the lag source as not bound without running anything. This module imports no real
backend adapter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_container_restarts import (
    HandlerContainerRestarts,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_container_restarts import (
    EnumContainerRestartsOutcome,
    ModelContainerRestartsCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelContainerRestartsSettings,
    ModelLabAlarmOverlay,
)

logger = logging.getLogger(__name__)


class ModelLabAlarmTickOutcome(NamedTuple):
    """How one scheduled lab alarm run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def _load_alarm_overlay(
    alarm_overlay_id: str, environ: Mapping[str, str] | None
) -> ModelLabAlarmOverlay:
    return load_structured_overlay(
        alarm_overlay_id,
        ModelLabAlarmOverlay,
        environ,
        file_env="ONEX_LAB_ALARM_OVERLAY",
    )


def run_lab_alarm_tick(
    *,
    alarm_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    handler: HandlerBrokerLag | None = None,
    build_handler: Callable[[ModelLabAlarmOverlay], HandlerBrokerLag] | None = None,
) -> ModelLabAlarmTickOutcome:
    """Run the lab alarm's broker-lag handler once, the way the runtime unit does."""
    overlay = _load_alarm_overlay(alarm_overlay_id, environ)
    if handler is None and build_handler is not None:
        handler = build_handler(overlay)
    if handler is None:
        return ModelLabAlarmTickOutcome(
            True,
            "the lab alarm's lag source is not bound in this runtime: "
            "no handler was supplied",
        )
    result = handler.handle(
        ModelBrokerLagCommand(
            correlation_id=f"lab-alarm-scheduled-{clock():%Y%m%dT%H%M%SZ}",
            write=True,
            fault=EnumLabAlarmFault.NONE,
        )
    )
    if (
        "delivery-failed" in result.failure
        or "delivery-unavailable" in result.failure
        or result.failure.startswith("record-unwritable")
    ):
        return ModelLabAlarmTickOutcome(
            True, f"the lab alarm run failed: {result.failure}"
        )
    if result.outcome is EnumBrokerLagOutcome.INDETERMINATE:
        reasons = ",".join(result.reasons) if result.reasons else "no reason named"
        return ModelLabAlarmTickOutcome(
            True, f"the lab alarm could not read the lag: {reasons}"
        )
    if result.outcome is EnumBrokerLagOutcome.ALARM:
        return ModelLabAlarmTickOutcome(
            False,
            f"outcome=alarm alarms={len(result.alarms)} delivered={len(result.delivery)}",
        )
    return ModelLabAlarmTickOutcome(
        False, f"outcome=ok declared={len(result.declared_groups)}"
    )


def run_container_restarts_probe_tick(
    *,
    alarm_overlay_id: str,
    environ: Mapping[str, str] | None,
    clock: Callable[[], datetime],
    build_handler: Callable[
        [ModelLabAlarmOverlay, ModelContainerRestartsSettings],
        HandlerContainerRestarts,
    ],
) -> ModelLabAlarmTickOutcome:
    """Read the declared restart counts without delivering an alarm."""
    overlay = _load_alarm_overlay(alarm_overlay_id, environ)
    settings = overlay.container_restarts
    if settings is None:
        return ModelLabAlarmTickOutcome(
            True, "container_restarts settings are missing from the lab alarm overlay"
        )
    result = build_handler(overlay, settings).handle(
        ModelContainerRestartsCommand(
            correlation_id=f"lab-alarm-container-restarts-{clock():%Y%m%dT%H%M%SZ}",
            write=False,
        )
    )
    detail = (
        f"container_restarts outcome={result.outcome.value} lane={result.census_lane} "
        f"readings={len(result.readings)} alarms={len(result.alarms)} "
        f"reasons={','.join(result.reasons) or 'none'}"
    )
    logger.info(
        "scheduled container_restarts probe outcome=%s readings=%d correlation_id=%s",
        result.outcome.value,
        len(result.readings),
        result.correlation_id,
    )
    return ModelLabAlarmTickOutcome(
        result.outcome is EnumContainerRestartsOutcome.INDETERMINATE,
        detail,
    )


__all__: list[str] = [
    "ModelLabAlarmTickOutcome",
    "NodeOverlayError",
    "run_container_restarts_probe_tick",
    "run_lab_alarm_tick",
]
