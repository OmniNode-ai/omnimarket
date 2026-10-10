# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the durable red-CI claims from the pr_lifecycle_ledger_entries projection.

node_pr_lifecycle_state_reducer projects every ci-red-triage-decided record into
the ledger: one decision row keyed by the decision's correlation id, and, for a
decision that started an owner, one owner claim row per member PR keyed by the
owner's correlation id. Reading those rows by exact key is what lets the triage
handler recognise a decided head, a claimed owner and a member absorbed into a
claimed cause after a runtime restart. This reader never writes: the reducer is
the projection's one writer.

An owner claim is a lease: a row owns only before its next_check_at, which the
reducer stamps from the TTL its contract declares. An expired claim (its owner
never closed, as a lane whose TERMINAL the ledger refused) owns nothing.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol

from omnimarket.models.ci_red_triage import (
    ci_red_claim_holds,
    ci_red_decision_correlation_id,
    ci_red_owner_correlation_id,
)
from omnimarket.projection.pr_ledger_projection import PR_LEDGER_PROJECTION_TABLE
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync


class CiRedClaimsUnreadError(RuntimeError):
    """The claim store could not be read; a start must be withheld."""


class ProtocolCiRedClaims(Protocol):
    def decided(self, decision_key: str) -> bool: ...
    def owned(self, owner_key: str) -> bool: ...
    def absorbing_cause(
        self, pr_number: int, cause_keys: Iterable[str]
    ) -> str | None: ...


class ProjectionCiRedClaims:
    """Exact-key reads of the ledger projection; every read failure raises."""

    def __init__(
        self,
        database: ProtocolProjectionDatabaseSync,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database = database
        self._now = now
        self._lock = threading.Lock()

    def _rows(self, filters: dict[str, object]) -> list[dict[str, object]]:
        try:
            with self._lock:
                return self._database.query(PR_LEDGER_PROJECTION_TABLE, filters)
        except Exception as exc:
            raise CiRedClaimsUnreadError(type(exc).__name__) from None

    def _held(self, filters: dict[str, object]) -> bool:
        """True when a claim row matching ``filters`` is inside its lease."""
        now = self._now()
        try:
            return any(
                ci_red_claim_holds(row.get("next_check_at"), now)
                for row in self._rows(filters)
            )
        except ValueError as exc:
            raise CiRedClaimsUnreadError(f"claim lease unread: {exc}") from None

    def decided(self, decision_key: str) -> bool:
        sweep_id = str(ci_red_decision_correlation_id(decision_key))
        return bool(self._rows({"sweep_id": sweep_id}))

    def owned(self, owner_key: str) -> bool:
        return self._held({"sweep_id": str(ci_red_owner_correlation_id(owner_key))})

    def absorbing_cause(self, pr_number: int, cause_keys: Iterable[str]) -> str | None:
        """The claimed cause that covers this PR, trying the given cause keys in order."""
        for cause_key in cause_keys:
            sweep_id = str(ci_red_owner_correlation_id(cause_key))
            if self._held({"sweep_id": sweep_id, "pr_number": pr_number}):
                return cause_key
        return None


class UnboundCiRedClaims:
    """No claim store is bound (its DSN variable is unset): every read is unread."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def decided(self, decision_key: str) -> bool:
        raise CiRedClaimsUnreadError(self._reason)

    def owned(self, owner_key: str) -> bool:
        raise CiRedClaimsUnreadError(self._reason)

    def absorbing_cause(self, pr_number: int, cause_keys: Iterable[str]) -> str | None:
        raise CiRedClaimsUnreadError(self._reason)


def claims_from_contract(block: dict[str, object]) -> ProtocolCiRedClaims:
    """Bind the read-only projection reader the contract's claims block names."""
    if block["table"] != PR_LEDGER_PROJECTION_TABLE:
        raise ValueError(f"claims table must be {PR_LEDGER_PROJECTION_TABLE}")
    dsn_env = str(block["dsn_env"])
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        return UnboundCiRedClaims(f"{dsn_env} unset")
    from omnimarket.projection.postgres_read_database import (
        PostgresReadDatabaseAdapter,
    )

    return ProjectionCiRedClaims(PostgresReadDatabaseAdapter(dsn))
