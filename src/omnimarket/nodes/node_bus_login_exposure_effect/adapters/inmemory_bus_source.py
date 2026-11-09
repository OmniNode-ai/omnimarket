# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory bus visibility source: answers with the overlay's fixture."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    ModelBusVisibilityRead,
)
from omnimarket.nodes.node_bus_login_exposure_effect.overlay import (
    ModelInMemoryFixture,
)


class InMemoryBusSource:
    def __init__(self, fixture: ModelInMemoryFixture) -> None:
        self._fixture = fixture

    def read(self, *, now: datetime) -> ModelBusVisibilityRead:
        fixture = self._fixture
        return ModelBusVisibilityRead(
            topics=fixture.topics,
            groups=fixture.groups,
            read_at=now - timedelta(seconds=fixture.read_age_seconds),
            read_failure=fixture.read_failure,
        )
