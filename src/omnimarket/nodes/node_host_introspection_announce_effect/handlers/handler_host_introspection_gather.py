# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostIntrospectionGather: read this host and return its announcement (OMN-19998).

Canonical def-B handler: ``handle(request: ModelHostIntrospectionProbeRequest) -> ModelHostIntrospectionAnnouncement``.
It reads the machine through the injected facts reader, takes the capacity advertisement from the existing capacity
effect (so the announcement's ``capacity`` is that handler's own reading, never a second implementation of it), reads
the host's row from the lab host table, and hands every reading to ``node_host_introspection_compute``. The time and the
announcement id are stamped here, never taken from the caller.

A reading that cannot be taken raises and produces no announcement; the announcer publishes the typed failure.
A lab host table that cannot be read is a failure too: it must not read as a host the table does not list.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_host_introspection_announcement import (
    ModelHostIntrospectionAnnouncement,
)
from omnimarket.models.model_host_introspection_request import (
    ModelHostCloneTarget,
    ModelHostIntrospectionOverlay,
    ModelHostIntrospectionRequest,
    ModelHostReadTargets,
    ModelHostRowFacts,
)
from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_host_introspection_announce_effect.models.model_host_introspection_probe_request import (
    ModelHostIntrospectionProbeRequest,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols.protocol_host_facts_reader import (
    HostFactsUnreadableError,
    ProtocolHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_compute.handlers.handler_host_introspection import (
    HandlerHostIntrospection,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityProbeRequest,
)


def find_row(table: ModelLabHostsTable, host_name: str) -> ModelLabHostRow | None:
    """The host's row: its ``name``, or one of the native hostnames the row declares."""
    for row in table.hosts:
        if row.name == host_name or host_name in row.hostnames:
            return row
    return None


class HandlerHostIntrospectionGather:
    """EFFECT handler: read this host, assemble its announcement."""

    def __init__(
        self,
        *,
        overlay: ModelHostIntrospectionOverlay,
        reader: ProtocolHostFactsReader,
        table: Callable[[], ModelLabHostsTable],
        capacity: HandlerHostCapacityAdvertiseEffect | None = None,
        compute: HandlerHostIntrospection | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._overlay = overlay
        self._reader = reader
        self._table = table
        self._capacity = capacity or HandlerHostCapacityAdvertiseEffect()
        self._compute = compute or HandlerHostIntrospection()
        self._now = now
        self._new_id = new_id

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(
        self, request: ModelHostIntrospectionProbeRequest
    ) -> ModelHostIntrospectionAnnouncement:
        try:
            table = self._table()
        except NodeOverlayError as exc:
            raise HostFactsUnreadableError(f"lab host table unreadable: {exc}") from exc
        row = find_row(table, request.host_name)
        targets = ModelHostReadTargets(
            host_name=request.host_name,
            model_port=row.model_port if row is not None else None,
            clones=tuple(
                ModelHostCloneTarget(name=clone.repo, path=clone.path)
                for clone in (row.clones if row is not None else ())
            ),
        )
        facts = self._reader.read(self._overlay, targets)
        capacity = self._capacity.handle(
            ModelHostCapacityProbeRequest(
                host_name=request.host_name,
                cadence_seconds=self._overlay.cadence_seconds,
                tools=list(self._overlay.capacity_tools),
                running_units=0,
                max_units=self._overlay.max_units,
                rank_penalty=row.penalty if row is not None else 0.0,
            )
        )
        return self._compute.handle(
            ModelHostIntrospectionRequest(
                host_name=request.host_name,
                reason=request.reason,
                announced_at=self._now(),
                announcement_id=self._new_id(),
                cadence_seconds=self._overlay.cadence_seconds,
                facts=facts,
                capacity=capacity,
                row=ModelHostRowFacts(
                    declared=row is not None,
                    roles=tuple(row.roles or ()) if row is not None else (),
                    available=row.available if row is not None else True,
                    unavailable_reason=row.unavailable_reason
                    if row is not None
                    else None,
                ),
                lane_accounts=self._overlay.lane_accounts,
            )
        )


__all__ = ["HandlerHostIntrospectionGather", "find_row"]
