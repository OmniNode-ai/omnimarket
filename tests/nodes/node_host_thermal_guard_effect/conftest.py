# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the host thermal guard tests: the example overlay, a fixed clock and the in-memory ports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
import yaml

from omnimarket.nodes.node_host_thermal_guard_effect.adapters import (
    InMemoryEpisodeStore,
    InMemoryHoldStore,
    InMemoryLaneCutter,
    InMemoryLedgerAppender,
    InMemorySensors,
)
from omnimarket.nodes.node_host_thermal_guard_effect.handlers import (
    HandlerHostThermalGuard,
)
from omnimarket.nodes.node_host_thermal_guard_effect.models import (
    ModelHostThermalGuardRequest,
    ModelThermalReading,
)
from omnimarket.nodes.node_host_thermal_guard_effect.overlay import (
    ModelHostThermalGuardOverlay,
)
from omnimarket.nodes.node_host_thermal_guard_effect.protocols import (
    ProtocolLedgerAppender,
)

NODE = "omnimarket.nodes.node_host_thermal_guard_effect"
START = datetime(2026, 11, 23, 21, 0, 0, tzinfo=UTC)


def example_overlay_text() -> str:
    return (files(NODE) / "overlay.example.yaml").read_text(encoding="utf-8")


@pytest.fixture
def overlay() -> ModelHostThermalGuardOverlay:
    return ModelHostThermalGuardOverlay.model_validate(
        yaml.safe_load(example_overlay_text())
    )


class Rig:
    """One guard over in-memory ports with a clock the test moves."""

    def __init__(self, overlay: ModelHostThermalGuardOverlay) -> None:
        self.overlay = overlay
        self.now = START
        self.holds = InMemoryHoldStore()
        self.episodes = InMemoryEpisodeStore()
        self.ledger = InMemoryLedgerAppender()
        self.appender: ProtocolLedgerAppender = self.ledger
        self.cutter = InMemoryLaneCutter()
        self.reading = ModelThermalReading("", 40.0, ())

    def run(
        self,
        hottest: float | None,
        after_s: int = 0,
        *,
        dry_run: bool = False,
        gpu: bool = False,
    ):
        self.now += timedelta(seconds=after_s)
        if hottest is None:
            reading = ModelThermalReading("", None, ())
        elif gpu:
            reading = ModelThermalReading("", 40.0, (hottest,))
        else:
            reading = ModelThermalReading("", hottest, ())
        handler = HandlerHostThermalGuard(
            InMemorySensors(reading),
            self.holds,
            self.episodes,
            self.appender,
            self.cutter,
            lambda: self.now,
        )
        return handler.handle(
            ModelHostThermalGuardRequest(
                host="lab-a", overlay=self.overlay, dry_run=dry_run
            )
        )


@pytest.fixture
def rig(overlay: ModelHostThermalGuardOverlay) -> Rig:
    return Rig(overlay)
