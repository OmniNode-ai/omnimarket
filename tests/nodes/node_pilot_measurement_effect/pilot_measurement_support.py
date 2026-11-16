# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures for the pilot measurement tests (OMN-23619).

One overlay home, judged by the real omnimarket models. A test breaks one thing by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_pilot_measurement_effect.adapters.inmemory_result_source import (
    InMemoryResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotMeasurementOverlay,
)
from omnimarket.nodes.node_pilot_measurement_effect.protocols import (
    PilotResultError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

SRC = Path(__file__).resolve().parents[3] / "src"
NODES = SRC / "omnimarket/nodes"
EXAMPLE = NODES / "node_pilot_measurement_effect/overlay.example.yaml"
RUN_AT = datetime(2026, 11, 6, 6, 40, tzinfo=UTC)
HOST = "example-host"


def example_overlay_document() -> dict[str, Any]:
    """The example overlay as a dict, loaded from overlay.example.yaml."""
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def overlay() -> ModelPilotMeasurementOverlay:
    return ModelPilotMeasurementOverlay.model_validate(example_overlay_document())


def _result(metrics: dict[str, float], measured_at: datetime) -> ModelPilotResultRead:
    return ModelPilotResultRead(metrics=metrics, measured_at=measured_at)


def _error() -> PilotResultError:
    return PilotResultError("result-unreadable")


def _results() -> dict[str, ModelPilotResultRead | PilotResultError]:
    """The two example pilots, both measured with a bar met (the golden case)."""
    return {
        "example-pilot-refactor": _result(
            {"example_success_rate": 0.95, "example_tokens_per_ticket": 30000.0}, RUN_AT
        ),
        "example-pilot-handoff": _result({"example_handoffs_per_ticket": 1.0}, RUN_AT),
    }


@dataclass
class World:
    """An overlay home with the example pilots, all accepted until a test breaks one."""

    backend: str = "memory"
    base: Path = field(default_factory=lambda: Path("/tmp"))
    results: dict[str, ModelPilotResultRead | PilotResultError] = field(
        default_factory=_results
    )
    ledger: InMemoryLedger | None = None

    def handler(
        self,
        ledger: InMemoryLedger | None = None,
        appender: Any = None,
        results: Any = None,
        clock: Any = None,
        emissions: Any = None,
        overlay_document: dict[str, Any] | None = None,
    ) -> HandlerPilotMeasurement:
        return HandlerPilotMeasurement(
            overlay=ModelPilotMeasurementOverlay.model_validate(
                overlay_document
                if overlay_document is not None
                else example_overlay_document()
            ),
            host=HOST,
            results=results
            if results is not None
            else InMemoryResultSource(self.results),
            appender=appender if appender is not None else ledger,
            clock=clock if clock is not None else (lambda: RUN_AT),
            emissions=emissions,
        )


__all__ = [
    "HOST",
    "RUN_AT",
    "World",
    "example_overlay_document",
    "overlay",
]
