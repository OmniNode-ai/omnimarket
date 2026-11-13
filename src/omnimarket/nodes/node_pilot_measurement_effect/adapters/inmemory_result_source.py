# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory result source tests inject: results by pilot name, or the error reading them."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import ModelPilotDeclaration
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError


class InMemoryResultSource:
    """Results keyed by pilot name; an entry that is a ``PilotResultError`` is raised as the read."""

    def __init__(
        self, results: Mapping[str, ModelPilotResultRead | PilotResultError]
    ) -> None:
        self._results: dict[str, ModelPilotResultRead | PilotResultError] = dict(
            results
        )

    def read(self, declaration: ModelPilotDeclaration) -> ModelPilotResultRead:
        result = self._results.get(declaration.pilot)
        if isinstance(result, PilotResultError):
            raise result
        if result is None:
            raise PilotResultError(f"no-result-declared-for-{declaration.pilot}")
        return result
