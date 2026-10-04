# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable, command-keyed claim on a delegate-skill command (OMN-18887).

Why this exists
---------------
The consume path runs broker-side auto-commit and never calls ``commit()``, so
the fetch position advances ahead of in-flight handlers and delivery is
at-least-once by contract. Since OMN-18852 four records run in flight at once.
A rebalance, a crash or a rewind therefore re-runs a delegation end to end: a
fresh inference is issued, the provider is called again, and a second billing
row is written.

Nothing noticed. ``descriptor.idempotent`` is ``false`` and the handler went
straight to dispatch with no lookup of any kind. It is also the first way this
system can double-bill without anything failing -- every prior cost surprise
was a failed rung retried by the escalation ladder, visible in ``attempts`` on
the terminal, whereas a redelivery produces a second, independent,
apparently-clean success.

Why a CLAIM and not a lookup
----------------------------
A ``SELECT`` followed by a dispatch has a window, and with four records in
flight in one process two deliveries of the same record really can both pass
it. The claim is therefore a single statement whose RETURN VALUE is the "did I
win" answer.

The primitive is the one the adapter contract already has:
``upsert_returning`` with ``insert_only_columns``. An UPSERT on its own cannot
say who won, because it always writes. Marking ``claimed_at`` insert-only
changes that: on a conflict the column is NOT overwritten, so the value that
comes back is the FIRST claimer's. Comparing it against the value this call
passed settles the race with no read-then-act window, in one round trip, on
either backend the resolver can return.

Why not the existing idempotency store
--------------------------------------
``omnibase_infra``'s ``idempotency_records`` is real, durable and purpose-built,
and it does not apply here for three independent reasons: the auto-wired
boundary this node uses is explicitly built without idempotency concerns, no
deployment supplies the config section that constructs the store, and its
suppression branch returns WITHOUT publishing a terminal -- which would convert
this double-bill into the missing-envelope defect OMN-15504 exists to prevent.
It is also keyed on envelope id rather than correlation id.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_reap_context import (
    ModelDelegationReapContext,
)
from omnimarket.projection.protocol_database import (
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    pass

CLAIMS_TABLE = "delegate_skill_command_claims"
REAP_CONTEXT_PREFIX = "reap:"
TERMINAL_SLOT_PREFIX = "slot:"
LATE_EVIDENCE_PREFIX = "late:"

# OMN-18887: the key is the DELIVERY, not the correlation.
#
# Correlation is the RETRY identity by construction. It defaults to a fresh
# uuid4 but a caller may supply one, and callers do reuse them -- this repo's
# own suite drives a forced failure and a success under a single correlation.
# Keyed on it, a reused correlation would be answered with the first run's
# stale terminal and never dispatched, which is a worse defect than the
# double-bill this port exists to prevent.
#
# The delivering record's identity separates the two cleanly. A REDELIVERY is
# the same record arriving twice and carries the same identity; a NEW command
# reusing a correlation is a different record and carries a different one. So
# a retry after a real failure still runs, which is the property the two
# halves have to preserve to compose with OMN-18916 on the grading side.
_DELIVERY_COLUMN = "delivery_id"
_CLAIMED_AT_COLUMN = "claimed_at"


@dataclass(frozen=True)
class ModelDelegationClaimOutcome:
    """The answer to "may I run this command?".

    ``won`` is the whole verdict. ``served_terminal`` is populated only on a
    loss and only once the original run has recorded one, which is why it is
    optional even when the claim was lost: a redelivery can arrive while the
    first attempt is still in flight, and the honest answer then is "somebody
    else owns this and has not finished".
    """

    won: bool
    served_terminal: dict[str, object] | None = None


@dataclass(frozen=True)
class ModelDelegationTerminalOutcome:
    """Slot verdict; a losing attempt must publish nothing."""

    won: bool
    held: dict[str, object] | None


@dataclass(frozen=True)
class ModelStalledDelegationClaim:
    """An overdue, unhanded command with a first-claim context."""

    delivery_id: UUID
    claimed_at: datetime
    context: ModelDelegationReapContext


@dataclass(frozen=True)
class ModelDelegationReapOutcome:
    """The held terminal to hand off, whether newly written or healed."""

    won: bool
    terminal: dict[str, object]


@runtime_checkable
class ProtocolDelegationIdempotencyPort(Protocol):
    """Claim a command and arbitrate its one terminal."""

    def claim(
        self,
        *,
        delivery_id: UUID,
        correlation_id: UUID,
        reap_context: ModelDelegationReapContext | None = None,
    ) -> ModelDelegationClaimOutcome: ...

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationTerminalOutcome: ...


class ProtocolDelegationReaperPort(Protocol):
    """Find overdue commands and hand off their slot winners."""

    def stalled_claims(
        self, *, now: datetime, limit: int
    ) -> list[ModelStalledDelegationClaim]: ...

    def reap(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationReapOutcome: ...


class _ProtocolClaimDatabase(
    ProtocolProjectionDatabaseSync, ProtocolProjectionAttestedWrite, Protocol
):
    """The claim store needs both attested writes and read-side queries."""


def _decode_terminal(raw: object) -> dict[str, object] | None:
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _require_attested_write(
    database: ProtocolProjectionDatabaseSync,
) -> _ProtocolClaimDatabase:
    # The claim needs `upsert_returning`, which lives on the NARROWER
    # attested-write protocol rather than on the sync protocol every
    # adapter implements. That separation is deliberate upstream, and the
    # documented contract for a caller that needs it is to probe and
    # refuse LOUDLY naming the adapter, rather than to assume. A claim
    # that silently could not be made would let the double-bill straight
    # through, which is the one outcome this port exists to prevent.
    if not isinstance(database, ProtocolProjectionAttestedWrite):
        raise TypeError(
            f"{type(database).__name__} does not implement "
            "ProtocolProjectionAttestedWrite, so a delegate-skill command "
            "claim cannot be made atomically and a redelivery would "
            "re-run and re-bill the inference (OMN-18887)"
        )
    return cast(_ProtocolClaimDatabase, database)


class DelegationClaimPort:
    """``ProtocolDelegationIdempotencyPort`` over the sync projection adapter.

    Backend-agnostic on purpose: the same code path serves the local SQLite
    evidence target and the Postgres substrate the projection binding overlay
    selects, so AC4's durability does not depend on which one is configured.
    """

    def __init__(
        self,
        database: ProtocolProjectionDatabaseSync | None = None,
        *,
        resolve_database: Callable[[], ProtocolProjectionDatabaseSync] | None = None,
    ) -> None:
        # OMN-19654: `resolve_database` defers choosing the backing store to
        # the first claim. A claim is only ever attempted for a bus delivery,
        # so the bus-less CLI -- which builds this port through the same
        # handler and never claims -- does not need a runtime state root to
        # exist, while a runtime that does claim is still refused, loudly, at
        # the claim, when it declares none. Exactly one of the two is given.
        if (database is None) == (resolve_database is None):
            raise TypeError(
                "DelegationClaimPort takes exactly one of `database` or "
                "`resolve_database`"
            )
        self._resolve_database = resolve_database
        self._resolved: _ProtocolClaimDatabase | None = (
            _require_attested_write(database) if database is not None else None
        )
        # Four records run in flight in one process (OMN-18852), so the first
        # claims on a fresh port can race. The deferred store is resolved
        # under this lock so exactly one adapter is built and every caller
        # claims through it.
        self._resolve_lock = threading.Lock()
        # Claims written before the reaper existed have no reap row and never
        # will; each is probed once per process instead of once per tick.
        self._without_context: set[str] = set()

    def _database(self) -> _ProtocolClaimDatabase:
        resolved = self._resolved
        if resolved is not None:
            return resolved
        with self._resolve_lock:
            if self._resolved is None:
                if self._resolve_database is None:
                    raise RuntimeError(
                        "DelegationClaimPort has neither a database nor a "
                        "resolver (OMN-19654)"
                    )
                self._resolved = _require_attested_write(self._resolve_database())
            return self._resolved

    def _insert_only(
        self,
        key: str,
        correlation_id: str,
        raw: str,
        mine: str,
    ) -> list[dict[str, object]]:
        # correlation_id keeps the conflict arm writable, so RETURNING still
        # reports the first writer when both arbiter columns are insert-only.
        return self._database().upsert_returning(
            CLAIMS_TABLE,
            _DELIVERY_COLUMN,
            {
                _DELIVERY_COLUMN: key,
                "correlation_id": correlation_id,
                _CLAIMED_AT_COLUMN: mine,
                "terminal_json": raw,
            },
            insert_only_columns=frozenset({_CLAIMED_AT_COLUMN, "terminal_json"}),
            returning=(_CLAIMED_AT_COLUMN, "terminal_json"),
        )

    def claim(
        self,
        *,
        delivery_id: UUID,
        correlation_id: UUID,
        reap_context: ModelDelegationReapContext | None = None,
    ) -> ModelDelegationClaimOutcome:
        mine = datetime.now(UTC).isoformat()
        if reap_context is not None:
            self._insert_only(
                f"{REAP_CONTEXT_PREFIX}{delivery_id}",
                str(correlation_id),
                reap_context.model_dump_json(),
                mine,
            )
        rows = self._insert_only(str(delivery_id), str(correlation_id), "", mine)
        if not rows:
            return ModelDelegationClaimOutcome(won=False)
        row = rows[0]
        if str(row.get(_CLAIMED_AT_COLUMN, "")) == mine:
            return ModelDelegationClaimOutcome(won=True)
        raw = row.get("terminal_json")
        if not raw:
            slots = self._database().query(
                CLAIMS_TABLE, {_DELIVERY_COLUMN: f"{TERMINAL_SLOT_PREFIX}{delivery_id}"}
            )
            raw = slots[0].get("terminal_json") if slots else None
        return ModelDelegationClaimOutcome(
            won=False, served_terminal=_decode_terminal(raw)
        )

    def _slot(
        self, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationTerminalOutcome:
        # A per-attempt token, not a bare instant: two attempts that read the
        # same clock tick would otherwise both see their own value come back
        # and both believe they won the one terminal slot.
        mine = f"{datetime.now(UTC).isoformat()}#{uuid4().hex}"
        data = terminal.get("data")
        correlation_id = (
            str(data.get("correlation_id", "")) if isinstance(data, dict) else ""
        )
        rows = self._insert_only(
            f"{TERMINAL_SLOT_PREFIX}{delivery_id}",
            correlation_id,
            json.dumps(terminal, default=str),
            mine,
        )
        if not rows:
            raise RuntimeError("Terminal slot write returned no arbitration verdict")
        row = rows[0]
        won = str(row.get(_CLAIMED_AT_COLUMN, "")) == mine
        return ModelDelegationTerminalOutcome(
            won=won,
            held=None if won else _decode_terminal(row.get("terminal_json")) or {},
        )

    def _copy_terminal(self, delivery_id: UUID, terminal: dict[str, object]) -> None:
        # The INSERT arm needs claimed_at even on a conflict; insert-only keeps
        # the real claim instant fixed while the handoff marker is healed.
        self._database().upsert_returning(
            CLAIMS_TABLE,
            _DELIVERY_COLUMN,
            {
                _DELIVERY_COLUMN: str(delivery_id),
                _CLAIMED_AT_COLUMN: datetime.now(UTC).isoformat(),
                "terminal_json": json.dumps(terminal, default=str),
            },
            insert_only_columns=frozenset({_CLAIMED_AT_COLUMN}),
        )

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationTerminalOutcome:
        try:
            outcome = self._slot(delivery_id, terminal)
        except RuntimeError:
            # No verdict came back. The dispatch is already billed, so fail
            # towards answering the caller with this terminal, the behaviour
            # before the slot existed, rather than losing it.
            return ModelDelegationTerminalOutcome(won=True, held=None)
        if outcome.won:
            self._copy_terminal(delivery_id, terminal)
        else:
            data = terminal.get("data")
            status = str(data.get("status", "")) if isinstance(data, dict) else ""
            correlation_id = (
                str(data.get("correlation_id", "")) if isinstance(data, dict) else ""
            )
            self._insert_only(
                f"{LATE_EVIDENCE_PREFIX}{delivery_id}:{status}:{uuid4().hex}",
                correlation_id,
                json.dumps(terminal, default=str),
                datetime.now(UTC).isoformat(),
            )
        return outcome

    def stalled_claims(
        self, *, now: datetime, limit: int
    ) -> list[ModelStalledDelegationClaim]:
        stalled: list[ModelStalledDelegationClaim] = []
        for row in self._database().query(CLAIMS_TABLE, {"terminal_json": ""}):
            key = str(row.get(_DELIVERY_COLUMN, ""))
            if key.startswith(
                (REAP_CONTEXT_PREFIX, TERMINAL_SLOT_PREFIX, LATE_EVIDENCE_PREFIX)
            ):
                continue
            if key in self._without_context:
                continue
            try:
                delivery_id = UUID(key)
                contexts = self._database().query(
                    CLAIMS_TABLE,
                    {_DELIVERY_COLUMN: f"{REAP_CONTEXT_PREFIX}{delivery_id}"},
                )
                if not contexts:
                    self._without_context.add(key)
                    continue
                context = ModelDelegationReapContext.model_validate_json(
                    str(contexts[0].get("terminal_json", ""))
                )
                claimed_at = datetime.fromisoformat(
                    str(contexts[0][_CLAIMED_AT_COLUMN])
                )
                if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
                    continue
                if context.deadline_at <= now:
                    stalled.append(
                        ModelStalledDelegationClaim(delivery_id, claimed_at, context)
                    )
            except Exception:
                # A legacy or unreadable context cannot establish a reap deadline.
                continue
        stalled.sort(key=lambda claim: claim.context.deadline_at)
        return stalled[: max(0, limit)]

    def reap(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> ModelDelegationReapOutcome:
        outcome = self._slot(delivery_id, terminal)
        held = terminal if outcome.won else outcome.held or {}
        if held:
            # An undecodable winner is left unmarked so the next tick retries.
            self._copy_terminal(delivery_id, held)
        return ModelDelegationReapOutcome(won=outcome.won, terminal=held)


def default_claim_db_path() -> Path:
    """The local claim store, under the runtime's declared state root.

    OMN-19654: this used to sit beside the evidence store, whose location is
    derived from ``Path.home()``. A deployed runtime pod has ``HOME=/`` on a
    read-only root filesystem, so on the onex-lab lane every bus delivery
    tried to create ``/.omninode`` and terminalized ``OSError: [Errno 30]
    Read-only file system``. A claim is only made for a bus delivery, i.e. by
    a running runtime, and a running runtime declares where its writable
    state lives, so the store is resolved from that declaration and raises
    :class:`~omnimarket.config.state_root.OnexStateRootUnconfiguredError`
    when there is none.

    It is still NOT the evidence file. Control state and evidence are
    different things and are kept in different places on purpose: sharing
    the evidence file would couple this store's lifecycle to the evidence
    target's, so clearing one to reset a local install would silently reopen
    the double-bill.
    """
    from omnimarket.config.state_root import resolve_onex_state_root

    state_root = resolve_onex_state_root(purpose="the delegate-skill claim store")
    return state_root / "delegation" / "delegation_claims.sqlite"


def resolve_delegation_claim_store() -> DelegationClaimPort:
    """Resolve the claim port against the configured substrate.

    Follows the projection binding overlay exactly as the evidence target
    does, so a deployment pointing delegation at Postgres gets its claims
    there too, and falls back to a local SQLite file of its own otherwise,
    under the runtime's declared state root (OMN-19654).
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
        _adapter_for_dsn,
    )
    from omnimarket.projection.runner import (
        projection_runtime_binding_from_overlay_env,
    )
    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    binding = projection_runtime_binding_from_overlay_env()
    if binding is None:
        # Deferred, so the state root is resolved (and refused if absent) at
        # the first claim rather than when the handler is built (OMN-19654).
        return DelegationClaimPort(
            resolve_database=lambda: SqliteDatabaseAdapter(default_claim_db_path())
        )
    # OMN-19661: pinned to the schema the contract declares. The table name
    # stays bare (the adapter refuses a dotted one), and no role or database on
    # a deployed lane sets a search_path, so without the pin every claim here
    # resolved to `public`, where the table does not exist.
    return DelegationClaimPort(
        _adapter_for_dsn(
            binding.resolve_database_url(), postgres_schema=claims_schema()
        )
    )


_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def claims_schema(contract_path: Path = _CONTRACT_PATH) -> str:
    """The schema ``contract.yaml`` declares for :data:`CLAIMS_TABLE`.

    Read from ``db_io.db_tables`` rather than restated here, so the relation
    the port writes and the relation the migration and the grant name cannot
    drift apart. Exactly one entry must declare the table, with a schema;
    anything else is refused, because a guessed schema is how a claim lands
    in a relation nobody granted or migrated.
    """
    import yaml

    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    tables = raw.get("db_io", {}).get("db_tables", []) if isinstance(raw, dict) else []
    declared = [
        entry
        for entry in tables
        if isinstance(entry, dict) and entry.get("name") == CLAIMS_TABLE
    ]
    if len(declared) != 1 or not str(declared[0].get("schema") or "").strip():
        raise ValueError(
            f"{contract_path} must declare exactly one db_io.db_tables entry for "
            f"{CLAIMS_TABLE!r} with a schema; found {declared!r}"
        )
    return str(declared[0]["schema"]).strip()


__all__ = [
    "CLAIMS_TABLE",
    "LATE_EVIDENCE_PREFIX",
    "REAP_CONTEXT_PREFIX",
    "TERMINAL_SLOT_PREFIX",
    "DelegationClaimPort",
    "ModelDelegationClaimOutcome",
    "ModelDelegationReapOutcome",
    "ModelDelegationTerminalOutcome",
    "ModelStalledDelegationClaim",
    "ProtocolDelegationIdempotencyPort",
    "ProtocolDelegationReaperPort",
    "claims_schema",
    "default_claim_db_path",
    "resolve_delegation_claim_store",
]
