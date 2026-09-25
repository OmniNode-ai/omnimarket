# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable, correlation-keyed claim on a delegate-skill command (OMN-18887).

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
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from omnimarket.projection.protocol_database import (
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    pass

CLAIMS_TABLE = "delegate_skill_command_claims"

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


@runtime_checkable
class ProtocolDelegationIdempotencyPort(Protocol):
    """Claim a correlation for execution, or report who already holds it."""

    def claim(
        self, *, delivery_id: UUID, correlation_id: UUID
    ) -> ModelDelegationClaimOutcome:
        """Atomically claim ``delivery_id``; the return value is the verdict."""
        ...

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> None:
        """Store the terminal so a later redelivery can be answered from it."""
        ...


def _require_attested_write(
    database: ProtocolProjectionDatabaseSync,
) -> ProtocolProjectionAttestedWrite:
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
    return database


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
        self._resolved: ProtocolProjectionAttestedWrite | None = (
            _require_attested_write(database) if database is not None else None
        )
        # Four records run in flight in one process (OMN-18852), so the first
        # claims on a fresh port can race. The deferred store is resolved
        # under this lock so exactly one adapter is built and every caller
        # claims through it.
        self._resolve_lock = threading.Lock()

    def _database(self) -> ProtocolProjectionAttestedWrite:
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

    def claim(
        self, *, delivery_id: UUID, correlation_id: UUID
    ) -> ModelDelegationClaimOutcome:
        mine = datetime.now(UTC).isoformat()
        rows = self._database().upsert_returning(
            CLAIMS_TABLE,
            _DELIVERY_COLUMN,
            {
                _DELIVERY_COLUMN: str(delivery_id),
                # Carried for diagnostics only. The key is the delivery; this
                # column is what lets a reader join a suppressed redelivery
                # back to the chain it belongs to.
                "correlation_id": str(correlation_id),
                _CLAIMED_AT_COLUMN: mine,
                "terminal_json": "",
            },
            # The claim itself. On a conflict these are NOT overwritten, so the
            # returned `claimed_at` is the first claimer's and the comparison
            # below is the race verdict rather than a guess.
            insert_only_columns=frozenset({_CLAIMED_AT_COLUMN, "terminal_json"}),
            returning=(_CLAIMED_AT_COLUMN, "terminal_json"),
        )
        if not rows:
            # No row came back at all. That is not a win: it is an answer this
            # port could not read, and treating an unreadable claim as "mine"
            # is how a double-bill gets through. Fail towards NOT dispatching
            # only when we can also answer the caller -- here we cannot, so the
            # honest outcome is a loss with no terminal, and the caller's own
            # in-flight branch decides.
            return ModelDelegationClaimOutcome(won=False, served_terminal=None)

        row = rows[0]
        if str(row.get(_CLAIMED_AT_COLUMN, "")) == mine:
            return ModelDelegationClaimOutcome(won=True)

        raw = row.get("terminal_json") or ""
        if not raw:
            return ModelDelegationClaimOutcome(won=False, served_terminal=None)
        try:
            decoded = json.loads(str(raw))
        except (TypeError, ValueError):
            return ModelDelegationClaimOutcome(won=False, served_terminal=None)
        if not isinstance(decoded, dict):
            return ModelDelegationClaimOutcome(won=False, served_terminal=None)
        return ModelDelegationClaimOutcome(won=False, served_terminal=decoded)

    def record_terminal(
        self, *, delivery_id: UUID, terminal: dict[str, object]
    ) -> None:
        # `claimed_at` is carried even though this call never means to change
        # it. The INSERT arm of an upsert is evaluated before the conflict is
        # resolved, so a row omitting a NOT NULL column is refused outright
        # rather than falling through to the update. Passing it insert-only
        # satisfies the INSERT arm while leaving the real claimer's timestamp
        # untouched on the arm that actually runs.
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
    return DelegationClaimPort(_adapter_for_dsn(binding.resolve_database_url()))


__all__ = [
    "CLAIMS_TABLE",
    "DelegationClaimPort",
    "ModelDelegationClaimOutcome",
    "ProtocolDelegationIdempotencyPort",
    "default_claim_db_path",
    "resolve_delegation_claim_store",
]
