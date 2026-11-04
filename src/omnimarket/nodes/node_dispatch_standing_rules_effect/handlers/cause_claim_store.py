# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay adapters for the atomic cause-key claim-store crossing."""

import sqlite3
import threading
from collections.abc import Mapping

from omnimarket.models.standing_rules.model_rule_cause_idle import (
    ModelCauseReservation,
    ModelCauseReservationResult,
)


class MemoryCauseClaimStore:
    def __init__(self) -> None:
        self._claims: dict[str, ModelCauseReservation] = {}
        self._lock = threading.Lock()

    def reserve(self, claim: ModelCauseReservation) -> ModelCauseReservationResult:
        with self._lock:
            inserted = claim.key not in self._claims
            return ModelCauseReservationResult(
                inserted=inserted, holder=self._claims.setdefault(claim.key, claim)
            )


class SqliteCauseClaimStore:
    """Durable unique-key insert; competing processes share the configured database."""

    def __init__(self, path: str, timeout: float) -> None:
        self._path = path
        self._timeout = timeout
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cause_claims (cause_key TEXT PRIMARY KEY, lane TEXT NOT NULL, request_id TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=self._timeout)

    def reserve(self, claim: ModelCauseReservation) -> ModelCauseReservationResult:
        with self._connect() as db:
            inserted = db.execute(
                "INSERT INTO cause_claims (cause_key, lane, request_id) VALUES (?, ?, ?) ON CONFLICT(cause_key) DO NOTHING",
                (claim.key, claim.lane, claim.request_id),
            )
            row = db.execute(
                "SELECT cause_key, lane, request_id FROM cause_claims WHERE cause_key = ?",
                (claim.key,),
            ).fetchone()
            if row is None:
                raise OSError("claim insert returned no holder")
            return ModelCauseReservationResult(
                inserted=inserted.rowcount == 1,
                holder=ModelCauseReservation(
                    key=row[0], lane=row[1], request_id=row[2]
                ),
            )


_memory_stores: dict[str, MemoryCauseClaimStore] = {}
_memory_lock = threading.Lock()


def memory_cause_claim_store(config: Mapping[str, str]) -> object:
    with _memory_lock:
        return _memory_stores.setdefault(config["namespace"], MemoryCauseClaimStore())


def sqlite_cause_claim_store(config: Mapping[str, str]) -> object:
    return SqliteCauseClaimStore(config["path"], float(config["timeout_seconds"]))
