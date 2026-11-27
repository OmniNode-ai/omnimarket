# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open launch work for the lab fill, read through ``ProtocolTicketService`` (OMN-26762).

The adapter is named by the ``ticket_source.adapter`` key of the ``node_lab_fill_selection_compute``
overlay and looked up in a registry: ``linear`` is the deployed adapter, ``in_memory`` serves tests.
A second adapter swaps in by the overlay alone. No snapshot file is read.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from omnibase_spi.protocols.services.protocol_ticket_service import (
    ProtocolTicketService,
)

from omnimarket.handlers.lab_fill_coordination import (
    CLOSED_STATUS_TYPES,
    CLOSED_STATUSES,
    blockers_from_linear,
    coordination,
    read_open_blockers,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
    not_configured_message,
)
from omnimarket.models.lab_fill.model_lab_fill_selection_overlay import (
    OVERLAY_ID,
    ModelLabFillSelectionOverlay,
)
from omnimarket.models.lab_fill.model_lab_fill_ticket_source import (
    ModelLabFillTicketSource,
)

AdapterFactory = Callable[[ModelLabFillTicketSource], ProtocolTicketService]


class InMemoryTicketService:
    """A ``ProtocolTicketService`` over a list of ticket dicts, for tests and the overlay's ``in_memory`` key."""

    def __init__(self, tickets: list[dict[str, Any]] | None = None) -> None:
        self._tickets = {str(t["id"]): dict(t) for t in tickets or []}

    async def create_ticket(
        self,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ticket_id = f"MEM-{len(self._tickets) + 1}"
        self._tickets[ticket_id] = {
            "id": ticket_id,
            "title": title,
            "description": description,
            "labels": list(labels or []),
            "assignee": assignee,
            "status": "Todo",
            "statusType": "unstarted",
        }
        return ticket_id

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return dict(self._tickets[ticket_id])

    async def update_ticket_status(self, ticket_id: str, status: str) -> bool:
        self._tickets[ticket_id]["status"] = status
        return True

    async def add_comment(self, ticket_id: str, body: str) -> str:
        self._tickets[ticket_id].setdefault("comments", []).append(body)
        return f"{ticket_id}-c{len(self._tickets[ticket_id]['comments'])}"

    async def list_tickets(
        self, filters: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _filtered(list(self._tickets.values()), filters)[:limit]

    async def get_ticket_status(self, ticket_id: str) -> str:
        return str(self._tickets[ticket_id].get("status", ""))

    async def open_blockers(self, ticket_ids: list[str]) -> dict[str, list[str]]:
        """Each ticket's blockers that are not closed now; a closed blocker drops out on the next read."""
        out: dict[str, list[str]] = {}
        for tid in ticket_ids:
            open_ids = [
                b
                for b in self._tickets.get(tid, {}).get("blocked_by", [])
                if not _closed_row(self._tickets.get(b, {}))
            ]
            if open_ids:
                out[tid] = open_ids
        return out

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float | None = None) -> None:
        return None


def _closed_row(row: dict[str, Any]) -> bool:
    return (
        row.get("statusType") in CLOSED_STATUS_TYPES
        or str(row.get("status", "")).lower() in CLOSED_STATUSES
    )


def _filtered(
    rows: list[dict[str, Any]], filters: dict[str, Any] | None
) -> list[dict[str, Any]]:
    f = filters or {}
    out = []
    for row in rows:
        if f.get("assignee") and row.get("assignee") != f["assignee"]:
            continue
        types = f.get("status_types")
        if types and row.get("statusType") not in types:
            continue
        labels = f.get("labels")
        if labels and not set(labels) & set(row.get("labels") or []):
            continue
        out.append(dict(row))
    return out


class LinearTicketService:
    """Read-only ``ProtocolTicketService`` over the signed-in user's open Linear issues.

    Reads with the Linear snapshot node's own query and transport (the key comes from the
    environment there); a write is refused, as a read-only role would be.
    """

    def __init__(self, page_size: int = 100, max_pages: int = 60) -> None:
        self._page_size = page_size
        self._max_pages = max_pages
        self.operator = ""

    def _read(self) -> list[dict[str, Any]]:
        from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
            http_post,
            read_snapshot,
        )

        snap = read_snapshot(http_post, self._page_size, self._max_pages)
        self.operator = snap.operator
        return [i.model_dump(by_alias=True) for i in snap.issues]

    async def list_tickets(
        self, filters: dict[str, Any] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = _filtered(await asyncio.to_thread(self._read), filters)
        return rows[:limit]

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        for row in await asyncio.to_thread(self._read):
            if row["id"] == ticket_id:
                return row
        raise KeyError(ticket_id)

    async def get_ticket_status(self, ticket_id: str) -> str:
        return str((await self.get_ticket(ticket_id)).get("status", ""))

    async def open_blockers(self, ticket_ids: list[str]) -> dict[str, list[str]]:
        from omnimarket.nodes.node_linear_snapshot_effect.handlers.handler_linear_snapshot import (
            http_post,
        )

        every = await asyncio.to_thread(
            blockers_from_linear, http_post, self._page_size, self._max_pages
        )
        return {t: every[t] for t in ticket_ids if t in every}

    async def create_ticket(
        self,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        raise PermissionError("the lab fill's Linear ticket service is read-only")

    async def update_ticket_status(self, ticket_id: str, status: str) -> bool:
        raise PermissionError("the lab fill's Linear ticket service is read-only")

    async def add_comment(self, ticket_id: str, body: str) -> str:
        raise PermissionError("the lab fill's Linear ticket service is read-only")

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float | None = None) -> None:
        return None


DEFAULT_REGISTRY: dict[str, AdapterFactory] = {
    "linear": lambda _cfg: LinearTicketService(),
    "in_memory": lambda _cfg: InMemoryTicketService(),
}


def ticket_source(environ: Mapping[str, str] | None = None) -> ModelLabFillTicketSource:
    """The overlay's ``ticket_source`` key; an overlay that declares none refuses naming the key."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelLabFillSelectionOverlay, environ)
    if overlay.ticket_source is None:
        raise NodeOverlayError(
            not_configured_message(OVERLAY_ID, f"{OVERLAY_ID}.ticket_source")
        )
    return overlay.ticket_source


def resolve_ticket_service(
    environ: Mapping[str, str] | None = None,
    registry: Mapping[str, AdapterFactory] | None = None,
) -> ProtocolTicketService:
    """The adapter the overlay names; an adapter the registry lacks refuses naming it."""
    cfg = ticket_source(environ)
    factories = DEFAULT_REGISTRY if registry is None else registry
    factory = factories.get(cfg.adapter)
    if factory is None:
        raise NodeOverlayError(
            f"{OVERLAY_ID}.ticket_source.adapter {cfg.adapter!r} is not one of {sorted(factories)}"
        )
    return factory(cfg)


def _run[T](coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def read_open_launch_issues(
    environ: Mapping[str, str] | None = None,
    registry: Mapping[str, AdapterFactory] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """(operator, open launch tickets) through the declared adapter's ``list_tickets``."""
    cfg = ticket_source(environ)
    service = resolve_ticket_service(environ, registry)
    filters: dict[str, Any] = {
        "assignee": cfg.assignee,
        "labels": list(cfg.labels),
        "status_types": list(cfg.status_types),
    }
    issues: list[dict[str, Any]] = _run(
        lambda: service.list_tickets(filters, cfg.limit)
    )
    rule = coordination(environ).dependencies
    if rule is not None and rule.skip_blocked:
        # OMN-26762: read the blockers now, so a ticket whose blocker closed is free on this read
        ids = [str(i["id"]) for i in issues]
        blocked = _run(lambda: read_open_blockers(service, ids))
        issues = [
            {**i, "blocked_by": list(blocked.get(str(i["id"]), []))} for i in issues
        ]
    return cfg.assignee, list(issues)


__all__ = [
    "DEFAULT_REGISTRY",
    "InMemoryTicketService",
    "LinearTicketService",
    "read_open_launch_issues",
    "resolve_ticket_service",
    "ticket_source",
]
