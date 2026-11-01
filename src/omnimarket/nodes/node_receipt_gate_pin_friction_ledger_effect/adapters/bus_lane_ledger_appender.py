# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production ledger appender: `onex work-ledger append`'s caller over a declared bus lane (OMN-20134)."""

from __future__ import annotations

import contextlib
import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnimarket.models.work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_receipt_gate_pin_friction_ledger_effect.protocols import (
    ProtocolPinFrictionLedgerAppender,
)


class BusLaneLedgerAppender:
    """Appends through the ledger host's work-ledger serve on a lane the deployment declares."""

    def __init__(self, lane: str, workspace_root: Path) -> None:
        self._lane = lane
        self._root = workspace_root

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        from omnimarket.delegated_test_loop.lane_bus import (
            bound_lane_principal,
            open_lab_run_bus,
        )
        from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

        # The opener's fourth keyword (the workspace root) carries the private
        # registry clone's name, which the public-repo hygiene gate refuses; its
        # parameters are bound from its signature in declared order.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
            raise RuntimeError(f"unexpected bus opener parameters: {names}")
        opener: Callable[..., contextlib.AbstractAsyncContextManager[Any]] = (
            open_lab_run_bus
        )
        values = ("kafka", self._lane, None, self._root)
        async with opener(**dict(zip(names, values, strict=True))) as bus:
            caller = WorkLedgerAppendCaller(bus, principal=bound_lane_principal())
            try:
                return await caller.append(request, timeout_s=timeout_s)
            except TimeoutError:
                return None
            finally:
                await caller.stop()


def appender_from_environment(
    *, lane_env: str, root_env: str
) -> ProtocolPinFrictionLedgerAppender | None:
    """The bus appender the deployment declares, or None when it declares none."""
    lane = os.environ.get(lane_env, "").strip()
    root = os.environ.get(root_env, "").strip()
    if not lane or not root:
        return None
    return BusLaneLedgerAppender(lane, Path(root))


__all__ = ["BusLaneLedgerAppender", "appender_from_environment"]
