# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_work backend's real sender: one lab work unit over the node's declared bus lane.

With a ``kafka_bootstrap`` and no lane, the broker address is stated directly instead:
``open_lab_run_bus`` then resolves the client's credentials from the process environment's own
broker login, and no lane declaration file is read.

The lane opens through the overlay store's opener (the broker and client transport resolve from
the lane config under the overlay's workspace root), and the send is
:class:`omnimarket.lab_work.bus.LabWorkCaller`'s dispatch, which publishes the unit and resolves
the host's receipt from a terminal. A caller of this node may already sit inside a running event
loop, so the async work runs on a worker thread with a loop of its own. A bus that cannot be
opened is not raised: the caller reads an infra_error receipt, which the dispatcher refuses on.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
from omnimarket.lab_work.bus import LabWorkCaller
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


def _open_lane(
    kafka_bootstrap: str | None,
) -> Callable[[str | None, Path | None], AbstractAsyncContextManager[Any]]:
    """The kafka transport: the lane's declared broker when a lane is named, the stated address
    otherwise."""

    def opener(lane: str | None, root: Path | None) -> AbstractAsyncContextManager[Any]:
        return open_lane_bus(
            bus="kafka", lane=lane, kafka_bootstrap=kafka_bootstrap, root=root
        )

    return opener


class SenderLabWorkBus:
    """ProtocolLabWorkSender over the overlay's declared bus lane, or to a stated broker
    address (OMN-24042)."""

    def __init__(
        self,
        bus_lane: str | None,
        workspace_root: str | None,
        open_bus: Callable[[str | None, Path | None], AbstractAsyncContextManager[Any]]
        | None = None,
        kafka_bootstrap: str | None = None,
    ) -> None:
        self._bus_lane = bus_lane
        self._root = (
            Path(os.path.expanduser(os.path.expandvars(workspace_root)))
            if workspace_root
            else None
        )
        self._open_bus = open_bus or _open_lane(kafka_bootstrap)

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        # asyncio.run on a worker thread: the caller's own loop, if any, stays untouched.
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                return pool.submit(asyncio.run, self._send(request)).result()
            except (LabRunBusError, OSError) as exc:
                return _infra_error(request, f"{type(exc).__name__}: {exc}")

    async def _send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        async with self._open_bus(self._bus_lane, self._root) as bus:
            caller = LabWorkCaller(bus)
            await caller.start()
            try:
                return await caller.dispatch(request)
            finally:
                await caller.stop()


def _infra_error(
    request: ModelLabWorkUnitRequest, detail: str
) -> ModelLabWorkUnitReceipt:
    return ModelLabWorkUnitReceipt(
        work_unit_id=request.work_unit_id,
        target_host=request.target_host,
        host="",
        repo=request.repo,
        commit_sha=request.commit_sha,
        lane=request.lane,
        kind=request.kind,
        status=EnumLabWorkUnitStatus.INFRA_ERROR,
        detail=detail[:2000],
    )
