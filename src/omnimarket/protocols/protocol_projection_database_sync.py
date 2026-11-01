# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sync projection database protocols.

``omnimarket.projection.protocol_database`` re-exports every name here, so
callers that import them from there keep working.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolProjectionDatabaseSync(Protocol):
    """Protocol for synchronous projection database operations.

    Disambiguated from the async ProtocolProjectionDatabase in
    omnibase_compat which serves projection runners.
    """

    def upsert(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
    ) -> bool:
        """UPSERT a row. Returns True on success."""
        ...

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Query rows from a table with optional filters, ordering and bound.

        OMN-17888. ``order_by`` / ``descending`` / ``limit`` are the minimal
        typed capability a projection handler needs to ask a BOUNDED question --
        "the latest row of this session" -- instead of reading every row it
        might be and sorting them in Python. Without it,
        ``HandlerProjectionSessionReplay.project`` re-read the WHOLE session on
        EVERY event, which is O(n^2) in session length and made the busiest
        live session (100,441 rows at 2026-09-07T15:53Z, growing ~3,029/hour)
        both the slowest and, once the runtime seam grew a 125,000-row budget,
        about eight hours from being refused outright.

        All three are keyword-only with defaults that reproduce the previous
        behaviour exactly, so every existing caller and every existing double is
        unchanged. ``descending`` without ``order_by`` is a REFUSAL, not a
        silent no-op: silently ignoring it hands a caller that asked for the
        newest row the oldest one, which is a wrong answer wearing the shape of
        a right one.
        """
        ...


@runtime_checkable
class ProtocolProjectionAttestedWrite(Protocol):
    """A store that can perform an ATTESTED write and report what it stored.

    OMN-18159. Deliberately a SEPARATE protocol rather than two more methods on
    :class:`ProtocolProjectionDatabaseSync`, for two reasons that both matter.

    The first is honest interface segregation: this is a strictly narrower
    capability, and not every sync target has it. SQLite has no connected
    principal to attest to, so a local evidence side-target cannot answer the
    question this protocol asks even in principle -- it can only record that
    an expression was requested.

    The second is blast radius. ``ProtocolProjectionDatabaseSync`` is
    ``runtime_checkable``, and a ``runtime_checkable`` protocol matches on
    method PRESENCE, so adding a method to it silently reclassifies every
    existing structural implementation as a non-implementation. Around twenty
    test doubles across this repo implement the three-argument ``upsert`` and
    nothing else; every projection handler that guards its injected adapter
    with ``isinstance(db, DatabaseAdapter)`` would have begun raising
    ``TypeError`` against all of them at once. Widening a structural protocol
    is not a local change, and the compiler does not tell you.

    A caller that NEEDS an attested write probes for this protocol and refuses
    loudly when it is absent, naming the adapter. That refusal is the correct
    behaviour rather than a defensive one: writing the row anyway would store
    a NULL ``writer_identity``, and a readback ordering on a column that is
    silently never stamped reports "nobody wrote this" in exactly the same
    shape as "this was written by an unscoped principal".
    """

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
        sql_expression_columns: Mapping[str, str] = MappingProxyType({}),
        returning: Sequence[str] = (),
        derive: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        input_columns: Sequence[str] = (),
        derived_columns: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """UPSERT a row and return the rows the database actually stored.

        OMN-18159. The sync twin of ``DelegationProjectionRunner
        ._dynamic_upsert``, carrying the same four capabilities under the same
        parameter names and the same refusals, so the two write paths of
        ``node_projection_delegation`` cannot disagree about what a write
        means. :func:`omnibase_core.models.projection.model_upsert_plan.build_upsert_plan`
        owns every decision; an implementation here owns only its driver's
        parameter binding.

        This is NOT a richer alias of :meth:`upsert`. The two answer different
        questions -- ``upsert`` answers "did the write succeed", this answers
        "what is now stored" -- and only the second can serve a column the
        DATABASE stamped, which is the property the writer attestation on
        ``delegation_events`` depends on. ``upsert`` keeps its ``bool`` because
        it has many callers for which the stored row is not the question, and
        widening its return type would change what ``if db.upsert(...)`` means
        at every one of them.

        ``tenant`` states which tenant THIS statement runs as, for the case
        where the row deliberately does not name one. Omitted, the tenant is
        resolved from ``row["tenant_id"]`` exactly as ``upsert`` resolves it,
        so one resolver serves both halves of the RLS policy comparison.

        ``returning`` names the columns to read back. Empty (the default)
        leaves the statement exactly as ``upsert`` would have composed it and
        returns an empty list. A statement that took a ``DO NOTHING`` arm also
        returns an empty list -- there is no stored row this call can describe,
        and inventing one would be a confident-empty failure inverted.
        """
        ...


# Backward-compat alias — existing code imports DatabaseAdapter
DatabaseAdapter = ProtocolProjectionDatabaseSync
