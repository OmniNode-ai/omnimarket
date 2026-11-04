# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic revision-family claims. SQLite is the durable lab binding, not the live ledger."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.item_state_check import (
    ModelItemClaim,
    ModelItemDelivery,
    ModelItemDispatchReceipt,
    ModelItemRecoveryPlan,
)


class MemoryItemRecoveryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claims: dict[str, ModelItemClaim] = {}
        self.notices: dict[str, ModelItemDelivery] = {}
        self.rows: list[str] = []

    def claim(self, plan: ModelItemRecoveryPlan) -> tuple[bool, ModelItemClaim]:
        with self._lock:
            prior = self.claims.get(plan.claim_key)
            if prior:
                return False, prior
            value = ModelItemClaim(plan=plan)
            self.claims[plan.claim_key] = value
            self.rows.extend(
                (
                    f"CLAIM cause={plan.cause}",
                    f"BUDGET family={plan.family} attempt={plan.attempt}",
                )
            )
            return True, value

    def latest(self, family: str) -> ModelItemClaim | None:
        with self._lock:
            values = [c for c in self.claims.values() if c.plan.family == family]
            return max(values, key=lambda c: c.plan.attempt) if values else None

    def settle(
        self, plan: ModelItemRecoveryPlan, receipt: ModelItemDispatchReceipt
    ) -> None:
        with self._lock:
            self.claims[plan.claim_key] = ModelItemClaim(plan=plan, receipt=receipt)

    def delivery(self, family: str) -> ModelItemDelivery | None:
        with self._lock:
            return self.notices.get(family)

    def notice(self, family: str, delivery: ModelItemDelivery) -> None:
        with self._lock:
            self.notices[family] = delivery
            self.rows.append(
                f"STATUS family={family} state={'asked' if delivery.kind == 'question' else 'queued'} seq={delivery.position} reason={delivery.reason}"
            )


class SqliteItemRecoveryStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS item_claims (cause TEXT PRIMARY KEY, family TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS item_deliveries (family TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS item_audit (row TEXT NOT NULL)")

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def claim(self, plan: ModelItemRecoveryPlan) -> tuple[bool, ModelItemClaim]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT payload FROM item_claims WHERE cause=?", (plan.claim_key,)
            ).fetchone()
            if existing:
                return False, ModelItemClaim.model_validate_json(existing[0])
            value = ModelItemClaim(plan=plan)
            db.execute(
                "INSERT INTO item_claims VALUES (?, ?, ?)",
                (plan.claim_key, plan.family, value.model_dump_json()),
            )
            db.executemany(
                "INSERT INTO item_audit VALUES (?)",
                [
                    (f"CLAIM cause={plan.cause}",),
                    (f"BUDGET family={plan.family} attempt={plan.attempt}",),
                ],
            )
            return True, value

    def latest(self, family: str) -> ModelItemClaim | None:
        with self._connection() as db:
            rows = db.execute(
                "SELECT payload FROM item_claims WHERE family=?", (family,)
            ).fetchall()
        values = [ModelItemClaim.model_validate_json(row[0]) for row in rows]
        return max(values, key=lambda c: c.plan.attempt) if values else None

    def settle(
        self, plan: ModelItemRecoveryPlan, receipt: ModelItemDispatchReceipt
    ) -> None:
        value = ModelItemClaim(plan=plan, receipt=receipt)
        with self._connection() as db:
            db.execute(
                "UPDATE item_claims SET payload=? WHERE cause=?",
                (value.model_dump_json(), plan.claim_key),
            )

    def delivery(self, family: str) -> ModelItemDelivery | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT payload FROM item_deliveries WHERE family=?", (family,)
            ).fetchone()
        return None if row is None else ModelItemDelivery.model_validate_json(row[0])

    def notice(self, family: str, delivery: ModelItemDelivery) -> None:
        with self._connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO item_deliveries VALUES (?, ?)",
                (family, delivery.model_dump_json()),
            )
            db.execute(
                "INSERT INTO item_audit VALUES (?)",
                (
                    f"STATUS family={family} state={'asked' if delivery.kind == 'question' else 'queued'} seq={delivery.position} reason={delivery.reason}",
                ),
            )

    @property
    def rows(self) -> list[str]:
        with self._connection() as db:
            return [
                row[0]
                for row in db.execute("SELECT row FROM item_audit ORDER BY rowid")
            ]


def memory_item_recovery_store(config: Mapping[str, str]) -> object:
    return MemoryItemRecoveryStore()


def sqlite_item_recovery_store(config: Mapping[str, str]) -> object:
    return SqliteItemRecoveryStore(config["path"])
