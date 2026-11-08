# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Profiles of the landing chain tests: the in-memory bus always, a broker when named."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.chain.landing_chain import (
    BOOTSTRAP_ENV,
    LANE_ENV,
    MUTATE_ENV,
    MUTATIONS,
    ModelLandingChainOverlay,
)

PROFILES = [
    pytest.param("inmemory", id="inmemory"),
    pytest.param(
        "redpanda", id="redpanda", marks=[pytest.mark.integration, pytest.mark.kafka]
    ),
]


@pytest.fixture(params=PROFILES)
def overlay(request: pytest.FixtureRequest) -> ModelLandingChainOverlay:
    chosen = ModelLandingChainOverlay.for_profile(request.param)
    if chosen.profile == "redpanda" and not (chosen.bootstrap or chosen.lane):
        pytest.skip(f"no broker: {LANE_ENV} and {BOOTSTRAP_ENV} name none")
    return chosen


@pytest.fixture(autouse=True)
def injected_break() -> Iterator[None]:
    """Run the whole suite under one named mutation, to read it fail for real."""
    name = os.environ.get(MUTATE_ENV)
    if not name:
        yield
        return
    with MUTATIONS[name]():
        yield
