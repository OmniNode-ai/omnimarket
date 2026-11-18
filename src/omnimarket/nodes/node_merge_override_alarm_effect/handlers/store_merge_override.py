# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's four tables over an injected DatabaseAdapter (in-memory by default, Postgres in the runtime)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelMergeObservation,
    ModelMergeOverrideFinding,
    ModelMergeOverrideReceipt,
    ModelMergeOverrideVerdictEvent,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

RECEIPTS = "merge_override_receipts"
PENDING = "merge_override_pending"
VERDICTS = "merge_override_verdicts"
OPEN_FINDINGS = "merge_override_open_findings"
_KEY = "repo_pr"


def repo_pr(repository: str, pr_number: int) -> str:
    return f"{repository}#{pr_number}"


class MergeOverrideStore:
    """Raises whatever the adapter raises; the handler turns that into detector_unreadable."""

    def __init__(self, database: DatabaseAdapter) -> None:
        self._db = database

    def record_receipt(self, receipt: ModelMergeOverrideReceipt) -> bool:
        """Keep the newest receipt per pull request; an older arrival never replaces it."""
        key = repo_pr(receipt.repository, receipt.pr_number)
        [*existing] = self._db.query(RECEIPTS, {_KEY: key}, limit=1)
        if existing and _as_datetime(existing[0]["observed_at"]) > receipt.observed_at:
            return False
        self._db.upsert(
            RECEIPTS,
            _KEY,
            {
                _KEY: key,
                "repository": receipt.repository,
                "pr_number": receipt.pr_number,
                "head_sha": receipt.head_sha,
                "operation": receipt.operation,
                "observed_at": receipt.observed_at,
                "correlation_id": str(receipt.correlation_id),
            },
        )
        return True

    def newest_receipt(
        self, repository: str, pr_number: int
    ) -> ModelMergeOverrideReceipt | None:
        rows = self._db.query(RECEIPTS, {_KEY: repo_pr(repository, pr_number)}, limit=1)
        if not rows:
            return None
        row = rows[0]
        return ModelMergeOverrideReceipt(
            repository=str(row["repository"]),
            pr_number=int(str(row["pr_number"])),
            head_sha=str(row["head_sha"]),
            operation=str(row["operation"]),
            observed_at=_as_datetime(row["observed_at"]),
            correlation_id=UUID(str(row["correlation_id"])),
        )

    def seen(self, repository: str, pr_number: int) -> bool:
        """A verdict or a pending read already exists for this merge."""
        key: dict[str, object] = {_KEY: repo_pr(repository, pr_number)}
        return bool(self._db.query(VERDICTS, key, limit=1)) or bool(
            self._db.query(PENDING, key, limit=1)
        )

    def begin_pending(
        self,
        observation: ModelMergeObservation,
        correlation_id: UUID,
        requested_at: datetime,
    ) -> None:
        self._db.upsert(
            PENDING,
            _KEY,
            {
                _KEY: repo_pr(observation.repository, observation.pr_number),
                "repository": observation.repository,
                "pr_number": observation.pr_number,
                "correlation_id": str(correlation_id),
                "source": observation.source,
                "merge_sha": observation.merge_sha,
                "requested_at": requested_at,
            },
        )

    def pending_by_correlation(self, correlation_id: UUID) -> dict[str, object] | None:
        """The pending read a correlation id belongs to, unless its verdict is already recorded."""
        rows = self._db.query(PENDING, {"correlation_id": str(correlation_id)}, limit=1)
        if not rows or self._has_verdict(str(rows[0][_KEY])):
            return None
        return rows[0]

    def pending_older_than(self, cutoff: datetime) -> list[dict[str, object]]:
        """Pending reads requested before the cutoff whose verdict is not yet recorded.

        The adapter has no delete verb: a pending row is closed by the verdict row
        for the same ``repo_pr``, which every read here checks.
        """
        return [
            row
            for row in self._db.query(PENDING, None)
            if _as_datetime(row["requested_at"]) < cutoff
            and not self._has_verdict(str(row[_KEY]))
        ]

    def _has_verdict(self, key: str) -> bool:
        return bool(self._db.query(VERDICTS, {_KEY: key}, limit=1))

    def record_verdict(self, verdict: ModelMergeOverrideVerdictEvent) -> None:
        self._db.upsert(
            VERDICTS,
            _KEY,
            {
                _KEY: repo_pr(verdict.repository, verdict.pr_number),
                "repository": verdict.repository,
                "pr_number": verdict.pr_number,
                "verdict": verdict.verdict.value,
                "head_sha": verdict.head_sha,
                "merge_commit_sha": verdict.merge_commit_sha,
                "merger": verdict.merger,
                "source": verdict.source,
                "receipt_seen": verdict.receipt_seen,
                "ruling_row_id": verdict.ruling_row_id,
                "reason": verdict.reason,
                "decided_at": verdict.decided_at,
            },
        )

    def open_finding(
        self, finding: ModelMergeOverrideFinding, opened_at: datetime
    ) -> None:
        self._db.upsert(
            OPEN_FINDINGS,
            "finding_key",
            {
                "finding_key": finding.idempotency_key,
                "repository": finding.repository,
                "pr_number": finding.pr_number,
                "head_sha": finding.head_sha,
                "verdict": finding.verdict.value,
                "reason": finding.reason,
                "opened_at": opened_at,
                "source": finding.source,
            },
        )

    def verdict_rows(self) -> list[dict[str, object]]:
        return self._db.query(VERDICTS, None, order_by="decided_at")


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


__all__ = [
    "OPEN_FINDINGS",
    "PENDING",
    "RECEIPTS",
    "VERDICTS",
    "MergeOverrideStore",
    "repo_pr",
]
