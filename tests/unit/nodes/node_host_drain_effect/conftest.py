# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of node_host_drain_effect: synthetic hosts, runners and a fixed clock; no deployment fact."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_host_drain_effect.adapters.fakes import (
    FakeRunnerProbe,
    FakeRunnerStopper,
    FixedLaneSource,
)
from omnimarket.nodes.node_host_drain_effect.adapters.stores import InMemoryDrainStore
from omnimarket.nodes.node_host_drain_effect.overlay import (
    ModelHostDrainOverlay,
    load_example_overlay,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


@pytest.fixture
def overlay() -> ModelHostDrainOverlay:
    return load_example_overlay()


@pytest.fixture
def sink() -> InMemoryEmissionSink:
    return InMemoryEmissionSink()


@pytest.fixture
def store() -> InMemoryDrainStore:
    return InMemoryDrainStore()


@pytest.fixture
def lanes() -> FixedLaneSource:
    return FixedLaneSource({"example-host-a": 2, "example-host-b": 0})


@pytest.fixture
def probe() -> FakeRunnerProbe:
    return FakeRunnerProbe({})


@pytest.fixture
def stopper(probe: FakeRunnerProbe) -> FakeRunnerStopper:
    return FakeRunnerStopper(probe)


def clock() -> datetime:
    return NOW
