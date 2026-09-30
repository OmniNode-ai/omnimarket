# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostCapacityAdvertiseEffect -- this pool host's capacity, as an
advertisement (OMN-20105).

Canonical def-B handler: ``handle(request: ModelHostCapacityProbeRequest) ->
ModelHostCapacityAdvertisement``. It is the only reader of load, cores and
memory in the lab work path; placement reads the published advertisements.

``advertised_at`` is stamped here from the handler's clock, never taken from
the caller (the request has no such field). An unreadable reading raises
:class:`HostCapacityUnreadableError` and produces no advertisement: a reader
downstream sees the host's advertisements go stale, which placement treats as
``could_not_check``, never as an idle host.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityAdvertisement,
    ModelHostCapacityProbeRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols.local_host_reader import (
    LocalHostReader,
    ProtocolHostReader,
)


class HandlerHostCapacityAdvertiseEffect:
    """EFFECT handler: read this host and return its advertisement."""

    def __init__(
        self,
        reader: ProtocolHostReader | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader or LocalHostReader()
        self._now = now

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelHostCapacityProbeRequest
    ) -> ModelHostCapacityAdvertisement:
        reading = self._reader.read(list(request.tools))
        return ModelHostCapacityAdvertisement(
            host_name=request.host_name,
            cores=reading.cores,
            load1=reading.load1,
            mem_available_bytes=reading.mem_available_bytes,
            tools=list(reading.tools),
            running_units=request.running_units,
            max_units=request.max_units,
            rank_penalty=request.rank_penalty,
            advertised_at=self._now(),
            cadence_seconds=request.cadence_seconds,
        )


__all__ = ["HandlerHostCapacityAdvertiseEffect"]
