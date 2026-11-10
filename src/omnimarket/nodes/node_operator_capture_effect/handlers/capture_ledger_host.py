# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capture host the work-ledger serve process runs beside its own (OMN-20905, OMN-16176)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    ProtocolCaptureLedgerHandler,
    accepted_ledger_appender,
    onex_delegate_runner,
    runner_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_serve import (
    DEFAULT_RETRY_SECONDS,
    PROMOTION_LANE_ENV,
    OperatorCaptureHost,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_operator_capture_effect import (
    HandlerOperatorCaptureEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerAppendRunner,
)

logger = logging.getLogger(__name__)


def ledger_host_capture(
    bus: ProtocolLabRunBus,
    *,
    ledger: Path,
    append_runner: ProtocolLedgerAppendRunner,
    append_handler: ProtocolCaptureLedgerHandler,
    bus_lane: str | None,
    retry_seconds: float = DEFAULT_RETRY_SECONDS,
    promotion_lane: str | None = None,
) -> OperatorCaptureHost:
    """The capture host the work-ledger serve process runs beside its own.

    It classifies through ``onex delegate`` on the serve process's own bus lane and appends
    through the serve process's own append handler as the operator principal, so every row is an
    accepted append command that passes the ledger's lock and guards, carries ``req=`` and
    ``via=bus:`` cells and is never re-published to the bus (OMN-16176). A serve process with no
    operator principal has no identity to append as: it falls back to its raw append runner, the
    rows land without those cells, and the fallback is logged.
    """
    host_name = append_handler.host_name
    principal = append_handler.writers.operator_principal
    if principal:
        append = accepted_ledger_appender(
            append_handler, principal=principal, host_name=host_name
        )
    else:
        logger.warning(
            "operator-capture on %s has no operator principal: rows append without req= and via=",
            host_name,
        )
        append = runner_ledger_appender(append_runner)
    handler = HandlerOperatorCaptureEffect(
        store_dir=capture_store.store_dir(),
        ledger_path=ledger,
        host_name=host_name,
        delegate=onex_delegate_runner(bus_lane),
        append=append,
        promotion_lane=promotion_lane or os.environ.get(PROMOTION_LANE_ENV) or None,
    )
    return OperatorCaptureHost(bus, handler, retry_seconds=retry_seconds)


__all__ = ["ledger_host_capture"]
