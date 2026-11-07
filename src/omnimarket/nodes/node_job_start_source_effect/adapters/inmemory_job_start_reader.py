# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory job-start reader: answers with the overlay's fixture."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    ModelJobStartRead,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    ModelInMemoryFixture,
)


class InMemoryJobStartReader:
    def __init__(self, fixture: ModelInMemoryFixture) -> None:
        self._fixture = fixture

    def read(
        self,
        *,
        repositories: tuple[str, ...],
        since: datetime,
        until: datetime,
        now: datetime,
    ) -> ModelJobStartRead:
        fixture = self._fixture
        read_at = now - timedelta(seconds=fixture.read_age_seconds)
        if fixture.read_failure is not None:
            return ModelJobStartRead(
                read_at=read_at,
                calls=0,
                read_failure=fixture.read_failure,
                failed_repository=fixture.failed_repository,
            )
        read = (
            repositories
            if fixture.repositories_read is None
            else fixture.repositories_read
        )
        return ModelJobStartRead(
            starts=fixture.starts,
            repositories_read=read,
            read_at=read_at,
            calls=0,
        )
