# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the record publication: the keyed record store and the gateway's idempotent check-run write."""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    ModelPublishedAttributionRecord,
    ModelRecordCheckRun,
)


class GatewayWriteError(Exception):
    """The gateway write failed; the record stays pending and the next invocation retries it."""


class ProtocolRecordStore(Protocol):
    """The record's keyed upsert, key ``(repository, nightly slot date)``. The status moves ``pending`` to ``published``."""

    def upsert_pending(self, record: ModelPublishedAttributionRecord) -> bool:
        """Create the row under its key when absent. Returns True only for the call that created it (atomic).

        A second record for a key that exists, whatever its content, is dropped: the first record of a night stands.
        """
        ...

    def unpublished(self) -> tuple[ModelPublishedAttributionRecord, ...]: ...

    def mark_published(self, key: str) -> None: ...

    def is_published(self, repository: str, slot_date: date) -> bool: ...

    def get(
        self, repository: str, slot_date: date
    ) -> ModelPublishedAttributionRecord | None: ...


class ProtocolRecordGateway(Protocol):
    """Writes one record as a check run on the tested commit through the repository gateway verbs.

    ``ensure_check_run`` is idempotent by ``(repo, head_sha, name)``: it reads the commit's check runs of that name
    first (``onex scm commit-check-runs``) and writes (``onex scm check-run-create``) only when none exists, because the
    write verb keeps only an in-process replay record and a restart could otherwise create a second check run.
    """

    def ensure_check_run(
        self, repo: str, check_run: ModelRecordCheckRun
    ) -> Literal["created", "exists"]: ...


__all__: list[str] = [
    "GatewayWriteError",
    "ProtocolRecordGateway",
    "ProtocolRecordStore",
]
