# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory group source: answers with the overlay's fixture."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_runner_group_exposure_effect.models.model_exposure_probe import (
    ModelGroupRead,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelInMemoryFixture,
)


class InMemoryGroupSource:
    def __init__(self, fixture: ModelInMemoryFixture) -> None:
        self._fixture = fixture

    def read(self, *, now: datetime) -> ModelGroupRead:
        fixture = self._fixture
        return ModelGroupRead(
            group=fixture.group,
            files=fixture.files,
            jobs=fixture.jobs,
            read_at=now - timedelta(seconds=fixture.read_age_seconds),
            calls=0,
            read_failure=fixture.read_failure,
        )
