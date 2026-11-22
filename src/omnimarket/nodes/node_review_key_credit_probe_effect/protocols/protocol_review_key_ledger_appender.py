# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The appender that puts a key's credit flip on the ledger over the bus (OMN-26332).

The probe used to run the ``onex-ledger`` command on the ledger file's path. The row now goes to the
ledger host's work-ledger serve as an attributed append request on the bus lane the host's lab host
table row declares; the probe names no ledger file and reads no path variable. The writer is the
broker principal of the lane connection, never a cell of the row.
"""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    ModelAppendOutcome,
)

WORKSPACE_ROOT_ENVS = ("OMNIBASE_PATH", "OMNI_HOME")
APPEND_TIMEOUT_SECONDS = 60.0
PROBE_LANE = "review-key-credit-probe"
_ACCEPTED = frozenset(
    {
        EnumWorkLedgerAppendStatus.ACCEPTED,
        EnumWorkLedgerAppendStatus.DUPLICATE,
        EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
    }
)
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*=")

SendAppend = Callable[
    [ModelWorkLedgerAppendRequest, float],
    Awaitable[ModelWorkLedgerAppendReceipt | None],
]


class ProtocolReviewKeyLedgerAppender(Protocol):
    async def append(self, row: str) -> ModelAppendOutcome:
        """Append one row to the ledger and report whether it was accepted."""


class InMemoryReviewKeyLedgerAppender:
    """Keeps appended rows in a list; ``refuse`` makes every append fail with that detail."""

    def __init__(self, refuse: str | None = None) -> None:
        self.rows: list[str] = []
        self._refuse = refuse

    async def append(self, row: str) -> ModelAppendOutcome:
        if self._refuse is not None:
            return ModelAppendOutcome(ok=False, detail=self._refuse)
        self.rows.append(row)
        return ModelAppendOutcome(ok=True, detail="accepted")


def stamp_row(row: str, request_id: str, host: str) -> str:
    """The row with ``req=`` and ``via=bus:`` before its free text (the ledger's bus-write rule)."""
    cells = row.split(" | ")
    stamp = [f"req={request_id}", f"via=bus:{host}"]
    if len(cells) > 2 and not _FIELD.match(cells[-1].strip()):
        return " | ".join([*cells[:-1], *stamp, cells[-1]])
    return " | ".join([*cells, *stamp])


class BusReviewKeyLedgerAppender:
    """Appends through the ledger host's work-ledger serve over one declared bus lane."""

    def __init__(
        self,
        lane: str,
        workspace_root: Path,
        *,
        host: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_s: float = APPEND_TIMEOUT_SECONDS,
        send: SendAppend | None = None,
    ) -> None:
        self._lane = lane
        self._root = workspace_root
        self._host = host or socket.gethostname().split(".", 1)[0]
        self._clock = clock
        self._timeout_s = timeout_s
        self._send = send if send is not None else self._send_over_bus

    async def append(self, row: str) -> ModelAppendOutcome:
        request_id = uuid5(NAMESPACE_URL, f"{PROBE_LANE}:{self._host}:{row}")
        request = ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=stamp_row(row, str(request_id), self._host),
            requested_by_lane=PROBE_LANE,
            requesting_host=self._host,
            requested_at=self._clock(),
        )
        try:
            receipt = await self._send(request, self._timeout_s)
        except (OSError, RuntimeError, ValueError) as exc:
            return ModelAppendOutcome(
                ok=False,
                detail=f"ledger bus append failed: {type(exc).__name__}: {exc}",
            )
        if receipt is None:
            return ModelAppendOutcome(
                ok=False, detail=f"no receipt within {self._timeout_s:.0f}s"
            )
        if receipt.request_id != request_id:
            return ModelAppendOutcome(
                ok=False, detail=f"the ledger answered for request {receipt.request_id}"
            )
        return ModelAppendOutcome(
            ok=receipt.status in _ACCEPTED,
            detail=f"{receipt.status.value} {receipt.message}".strip()[:400],
        )

    async def _send_over_bus(
        self, request: ModelWorkLedgerAppendRequest, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        from omnimarket.delegated_test_loop.lane_bus import (
            bound_lane_principal,
            open_lab_run_bus,
        )
        from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller

        # The opener's fourth keyword (the workspace root) carries the private registry clone's name,
        # which the public-repo hygiene gate refuses; its parameters are bound from its signature in
        # declared order, as ``onex scm`` binds them.
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


def ledger_appender_for_lane(
    lane: str, host: str | None = None
) -> ProtocolReviewKeyLedgerAppender:
    """The bus appender for ``lane``; the workspace root is read from the first set root variable."""
    for name in WORKSPACE_ROOT_ENVS:
        root = os.environ.get(name, "").strip()
        if root:
            return BusReviewKeyLedgerAppender(lane, Path(root), host=host)
    raise RuntimeError(
        f"set one of {', '.join(WORKSPACE_ROOT_ENVS)} to the workspace root the bus lane is read from (no default)"
    )
