# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependencies, claims and the done gate for the lab fill, each behind the selection overlay (OMN-26762).

* Dependencies: a ticket's blockers are read from Linear on every read (``inverseRelations`` of type
  ``blocks``), and one counts only while its state is open, so a ticket unblocks itself when its blocker
  closes; nothing is stored.
* Claims: placements take an item through ``ProtocolLabFillClaimLock``. The adapter is named by
  ``coordination.claim_lock.adapter``; the shared one is the ledger's, never a file on a host.
* Done gate: ``GatedTicketService`` refuses the move to a done status unless the ticket's local done-gate
  receipt carries each declared item as VERIFIED.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from omnibase_spi.protocols.services.protocol_ticket_service import (
    ProtocolTicketService,
)

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_fill.model_lab_fill_coordination import (
    ModelLabFillClaimLockConfig,
    ModelLabFillCoordination,
    ModelLabFillDoneGate,
)
from omnimarket.models.lab_fill.model_lab_fill_selection_overlay import (
    OVERLAY_ID,
    ModelLabFillSelectionOverlay,
)
from omnimarket.models.model_local_done_gate_receipt import (
    ModelLocalDoneGateReceipt,
)

CLOSED_STATUS_TYPES = frozenset({"completed", "canceled", "cancelled"})
CLOSED_STATUSES = frozenset({"done", "canceled", "cancelled", "duplicate"})

_RELATIONS_QUERY = """
query LabFillBlockers($first: Int!, $after: String) {
  issues(
    first: $first
    after: $after
    filter: {
      team: { key: { eq: "OMN" } }
      assignee: { isMe: { eq: true } }
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      inverseRelations(first: 50) {
        nodes { type issue { identifier state { type } } }
      }
    }
  }
}
"""


def coordination(environ: Mapping[str, str] | None = None) -> ModelLabFillCoordination:
    """The overlay's ``coordination`` block; one that declares none turns every rule off."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelLabFillSelectionOverlay, environ)
    return overlay.coordination or ModelLabFillCoordination()


# ---- AC5: dependencies --------------------------------------------------------------------------------------------


@runtime_checkable
class ProtocolTicketBlockers(Protocol):
    """A ticket service that answers each ticket's open blockers; Linear's and the in-memory one do."""

    async def open_blockers(self, ticket_ids: list[str]) -> dict[str, list[str]]: ...


def blockers_from_linear(
    post: Callable[[str, dict[str, Any]], dict[str, Any]],
    page_size: int = 100,
    max_pages: int = 60,
) -> dict[str, list[str]]:
    """{ticket id: ids of its open blockers} for the signed-in user's open issues, read from Linear's relations."""
    out: dict[str, list[str]] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        page = (post(_RELATIONS_QUERY, {"first": page_size, "after": cursor})).get(
            "issues"
        ) or {}
        for node in page.get("nodes") or []:
            blockers = [
                str(rel["issue"]["identifier"])
                for rel in (node.get("inverseRelations") or {}).get("nodes") or []
                if rel.get("type") == "blocks"
                and ((rel.get("issue") or {}).get("state") or {}).get("type")
                not in CLOSED_STATUS_TYPES
            ]
            if blockers:
                out[str(node["identifier"])] = blockers
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        cursor = info.get("endCursor")
    raise RuntimeError(f"the relations read had a next page after {max_pages} pages")


async def read_open_blockers(
    service: ProtocolTicketService, ticket_ids: list[str]
) -> dict[str, list[str]]:
    """The open blockers of each ticket, read through the service now; a service that cannot answer refuses."""
    if not isinstance(service, ProtocolTicketBlockers):
        raise NodeOverlayError(
            f"{OVERLAY_ID}.coordination.dependencies.skip_blocked needs an adapter that reads "
            f"dependencies; {type(service).__name__} does not"
        )
    return await service.open_blockers(ticket_ids)


# ---- AC6: claims under a shared lock ------------------------------------------------------------------------------


class ProtocolLabFillClaimLock(Protocol):
    """Takes one item for one holder; a second holder is refused until the claim lapses or is released."""

    def try_claim(self, key: str, holder: str, ttl_seconds: int, now: str) -> bool: ...

    def release(self, key: str, holder: str) -> None: ...


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class InMemoryClaimLock:
    """A claim lock in process memory, for tests and the overlay's ``in_memory`` key; it writes no file."""

    def __init__(self) -> None:
        self._claims: dict[str, tuple[str, datetime]] = {}

    def try_claim(self, key: str, holder: str, ttl_seconds: int, now: str) -> bool:
        held = self._claims.get(key)
        if held is not None and held[0] != holder and held[1] > _at(now):
            return False
        self._claims[key] = (holder, _at(now) + timedelta(seconds=ttl_seconds))
        return True

    def release(self, key: str, holder: str) -> None:
        if self._claims.get(key, ("", _at("1970-01-01T00:00:00Z")))[0] == holder:
            del self._claims[key]


class ProtocolLedgerPort(Protocol):
    """The shared ledger as the claim lock sees it: its rows, and an append."""

    def read_rows(self) -> list[str]: ...

    def append_row(self, row: str) -> None: ...


def _cells(row: str) -> dict[str, str]:
    parts = [p.strip() for p in row.split("|")]
    cells = {"ts": parts[0], "type": parts[1] if len(parts) > 1 else ""}
    for p in parts[2:]:
        if "=" in p:
            k, _, v = p.partition("=")
            cells[k.strip()] = v.strip()
    return cells


class LedgerClaimLock:
    """The claim lock kept on the shared ledger: a claim is a STATUS row naming ``claim-key`` with ``claim-state=taken``, a release one with ``released``.

    The first live row for a key owns it; after appending, the lock reads back and keeps the claim only when
    its own row is that first one, so two placements racing for one item cannot both win.
    """

    def __init__(self, port: ProtocolLedgerPort, lane_prefix: str) -> None:
        self._port = port
        self._prefix = lane_prefix

    def _owner(self, key: str, now: str) -> str | None:
        owner: str | None = None
        expires = _at("1970-01-01T00:00:00Z")
        for row in self._port.read_rows():
            c = _cells(row)
            if c.get("claim-key") != key:
                continue
            if c.get("claim-state") == "released":
                owner = None
            elif c.get("claim-state") == "taken":
                at = _at(c["ts"])
                live = owner is not None and expires > _at(now)
                if owner is None or not live or owner == c.get("holder"):
                    owner = c.get("holder")
                    expires = at + timedelta(seconds=int(c.get("ttl", "0")))
        return owner if owner is not None and expires > _at(now) else None

    def try_claim(self, key: str, holder: str, ttl_seconds: int, now: str) -> bool:
        owner = self._owner(key, now)
        if owner is not None and owner != holder:
            return False
        self._port.append_row(
            f"{now} | STATUS | lane={self._prefix} | holder={holder} | claim-key={key} | claim-state=taken "
            f"| ttl={ttl_seconds} | lab fill placement claim"
        )
        return self._owner(key, now) == holder

    def release(self, key: str, holder: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._port.append_row(
            f"{now} | STATUS | lane={self._prefix} | holder={holder} | claim-key={key} | claim-state=released "
            f"| lab fill claim released"
        )


class BusLedgerPort:
    """The rolling ledger as the lock sees it: rows read from the bus-fed work ledger query node (never the file), rows
    appended by ``onex-ledger``. A read that did not come is an error, so the lock refuses rather than guess."""

    def __init__(
        self, cfg: ModelLabFillClaimLockConfig, environ: Mapping[str, str]
    ) -> None:
        for key in ("reader", "ledger_path_env"):
            if not getattr(cfg, key):
                raise NodeOverlayError(
                    f"{OVERLAY_ID}.coordination.claim_lock.{key} is empty; the ledger adapter needs it"
                )
        self._cfg = cfg
        self._environ = environ

    def read_rows(self) -> list[str]:
        from omnimarket.models.model_work_ledger_reader import (
            EnumReaderQuery,
            EnumReaderStatus,
            ModelReaderRequest,
        )
        from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
            InMemoryEventSink,
            build_point_read,
        )
        from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
            HandlerWorkLedgerReader,
        )
        from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
            load_overlay as load_reader_overlay,
        )

        overlay = load_reader_overlay(self._environ)
        reader = HandlerWorkLedgerReader(
            overlay,
            build_point_read(overlay.route, self._environ),
            InMemoryEventSink(),
        )
        answer = reader.handle(
            ModelReaderRequest(
                reader=self._cfg.reader,
                query=EnumReaderQuery.ROWS,
                window_hours=max(1, -(-self._cfg.ttl_seconds // 3600)),
                kinds=("STATUS",),
                term="claim-key=",
            )
        )
        if answer.status not in (EnumReaderStatus.OK, EnumReaderStatus.STALE):
            raise RuntimeError(
                f"the ledger claim read did not come: {answer.status} {answer.reason}"
            )
        return list(answer.lines)

    def append_row(self, row: str) -> None:
        path = self._environ.get(self._cfg.ledger_path_env, "")
        if not path:
            raise NodeOverlayError(
                f"the ledger claim lock needs {self._cfg.ledger_path_env} set to the ledger it appends to"
            )
        subprocess.run(["onex-ledger", path, "--append", row], check=True, timeout=60)


ClaimLockFactory = Callable[
    [ModelLabFillClaimLockConfig, Mapping[str, str]], ProtocolLabFillClaimLock
]

DEFAULT_CLAIM_LOCKS: dict[str, ClaimLockFactory] = {
    "in_memory": lambda _cfg, _env: InMemoryClaimLock(),
    "ledger": lambda cfg, env: LedgerClaimLock(
        BusLedgerPort(cfg, env), "lab-fill-claim"
    ),
}


def resolve_claim_lock(
    environ: Mapping[str, str] | None = None,
    registry: Mapping[str, ClaimLockFactory] | None = None,
) -> ProtocolLabFillClaimLock | None:
    """The claim lock the overlay names; None when it declares none, an adapter the registry lacks refuses naming it."""
    cfg = coordination(environ).claim_lock
    if cfg is None:
        return None
    factories = DEFAULT_CLAIM_LOCKS if registry is None else registry
    factory = factories.get(cfg.adapter)
    if factory is None:
        raise NodeOverlayError(
            f"{OVERLAY_ID}.coordination.claim_lock.adapter {cfg.adapter!r} is not one of {sorted(factories)}"
        )
    return factory(cfg, os.environ if environ is None else environ)


def resolve_claim(
    environ: Mapping[str, str] | None = None,
) -> Callable[[str, str, str], bool] | None:
    """``claim(key, holder, now)`` over the overlay's lock and ttl; None when no overlay or no lock is declared."""
    try:
        cfg = coordination(environ).claim_lock
    except NodeOverlayError:
        return None  # an unreadable overlay is reported by the ticket read; it declares no lock here
    lock = resolve_claim_lock(environ)
    if cfg is None or lock is None:
        return None
    return lambda key, holder, now: lock.try_claim(key, holder, cfg.ttl_seconds, now)


# ---- AC7: a declared gate on the move to done ---------------------------------------------------------------------


def done_gate_refusal(
    ticket_id: str,
    receipt: ModelLocalDoneGateReceipt | None,
    gate: ModelLabFillDoneGate,
) -> str | None:
    """Why the ticket may not move to done, or None when every declared item reads VERIFIED."""
    if receipt is None or receipt.ticket_id != ticket_id:
        return f"{ticket_id} has no local done-gate receipt; the gate needs {', '.join(gate.required_items)}"
    verdicts = {i.item_id: i.verdict for i in receipt.items}
    bad = [
        f"{item}={verdicts.get(item, 'absent')}"
        for item in gate.required_items
        if verdicts.get(item) != "VERIFIED"
    ]
    return f"{ticket_id} done gate: {', '.join(bad)}" if bad else None


class GatedTicketService:
    """A ``ProtocolTicketService`` that refuses the move to a done status until the declared gate reads verified."""

    def __init__(
        self,
        inner: ProtocolTicketService,
        gate: ModelLabFillDoneGate,
        receipt_for: Callable[[str], ModelLocalDoneGateReceipt | None],
    ) -> None:
        self._inner = inner
        self._gate = gate
        self._receipt_for = receipt_for

    async def update_ticket_status(self, ticket_id: str, status: str) -> bool:
        if status in self._gate.done_statuses:
            why = done_gate_refusal(ticket_id, self._receipt_for(ticket_id), self._gate)
            if why is not None:
                raise PermissionError(why)
        return await self._inner.update_ticket_status(ticket_id, status)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


__all__ = [
    "DEFAULT_CLAIM_LOCKS",
    "BusLedgerPort",
    "GatedTicketService",
    "InMemoryClaimLock",
    "LedgerClaimLock",
    "ProtocolLabFillClaimLock",
    "ProtocolTicketBlockers",
    "blockers_from_linear",
    "coordination",
    "done_gate_refusal",
    "read_open_blockers",
    "resolve_claim",
    "resolve_claim_lock",
]
