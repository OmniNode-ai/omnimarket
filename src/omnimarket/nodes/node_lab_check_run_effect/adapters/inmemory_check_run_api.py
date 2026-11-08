# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory check-run API: the default backend; keeps runs the way the real API does, by id."""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)
from omnimarket.nodes.node_lab_check_run_effect.protocols.protocol_check_run_api import (
    CheckRunApiError,
)


class InMemoryCheckRunApi:
    def __init__(self, fixture: Iterable[ModelCheckRunRecord] = ()) -> None:
        self._runs: dict[int, ModelCheckRunRecord] = {
            r.check_run_id: r for r in fixture
        }
        self._next_id = max(self._runs, default=0) + 1
        self._calls = 0

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def runs(self) -> tuple[ModelCheckRunRecord, ...]:
        return tuple(self._runs[i] for i in sorted(self._runs))

    def list_runs(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelCheckRunRecord, ...]:
        self._calls += 1
        return tuple(
            r
            for r in self.runs
            if r.repository == repository
            and r.head_sha == head_sha
            and r.name == check_name
        )

    def create_run(
        self,
        *,
        repository: str,
        head_sha: str,
        name: str,
        attempt_id: str,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord:
        self._calls += 1
        record = ModelCheckRunRecord(
            check_run_id=self._next_id,
            repository=repository,
            name=name,
            head_sha=head_sha,
            attempt_id=attempt_id,
            status=status,
            conclusion=conclusion,
        )
        self._runs[record.check_run_id] = record
        self._next_id += 1
        return record

    def update_run(
        self,
        *,
        repository: str,
        check_run_id: int,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord:
        self._calls += 1
        current = self._runs.get(check_run_id)
        if current is None or current.repository != repository:
            raise CheckRunApiError(f"check run {check_run_id} not found")
        updated = current.model_copy(
            update={"status": status, "conclusion": conclusion}
        )
        self._runs[check_run_id] = updated
        return updated
