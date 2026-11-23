# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's broker-lag run on its command topic (OMN-26509).

HandlerBrokerLag takes the overlay, the lag source and the sample store as constructor parameters, so the runtime's
auto-wiring cannot build it, and a contract that declared no subscribe topic was skipped. This handler has no required
constructor parameter: it reads the runtime's alarm overlay, builds the lag handler the overlay's backend names, runs it
on the command and writes one run line (outcome and the time it finished) to the runtime log. The scheduled node runs the
same alarm on the runtime tick and is the only tick consumer, so a tick never runs it twice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    ModelBrokerLagCommand,
    ModelBrokerLagResult,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_handler as build_lab_alarm_handler,
)

logger = logging.getLogger(__name__)

RUNTIME_ALARM_OVERLAY_ID = "node_lab_alarm_runtime"


class HandlerBrokerLagCommand:
    """Run one broker-lag read for a command that arrived on the node's command topic."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        build_handler: Callable[
            [ModelLabAlarmOverlay], HandlerBrokerLag
        ] = build_lab_alarm_handler,
    ) -> None:
        self._environ = environ
        self._clock = clock
        self._build_handler = build_handler

    def handle(self, request: ModelBrokerLagCommand) -> ModelBrokerLagResult:
        overlay = load_structured_overlay(
            RUNTIME_ALARM_OVERLAY_ID,
            ModelLabAlarmOverlay,
            self._environ,
            file_env="ONEX_LAB_ALARM_OVERLAY",
        )
        result = self._build_handler(overlay).handle(request)
        logger.info(
            "node_lab_alarm: broker-lag run outcome=%s finished_at=%s alarms=%d correlation_id=%s",
            result.outcome.value,
            self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            len(result.alarms),
            request.correlation_id,
        )
        return result


__all__: list[str] = ["RUNTIME_ALARM_OVERLAY_ID", "HandlerBrokerLagCommand"]
