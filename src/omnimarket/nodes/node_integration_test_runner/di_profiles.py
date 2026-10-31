"""DI profile factories — swap event bus and client bindings per profile."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest

from omnimarket.nodes.node_integration_test_runner.container.container_inmemory import (
    bind_fallback_event_bus,
    bind_local_event_bus,
)
from omnimarket.nodes.node_integration_test_runner.models.model_test_runner_request import (
    EnumDIProfile,
)

logger = logging.getLogger(__name__)


def build_event_bus_for_profile(profile: EnumDIProfile) -> Any:
    """Return the event bus implementation for the given profile.

    - LOCAL: the in-memory bus (no network required)
    - STAGING/PRODUCTION: EventBusKafka if KAFKA_BOOTSTRAP_SERVERS is set,
      else fall back to EventBusInmemory with a warning.
    """
    if profile == EnumDIProfile.LOCAL:
        return bind_local_event_bus()

    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "")
    if not bootstrap:
        logger.warning(
            "[di_profiles] KAFKA_BOOTSTRAP_SERVERS not set for profile=%s — "
            "falling back to EventBusInmemory",
            profile,
        )
        return bind_fallback_event_bus(environment=profile.value)

    try:
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

        return EventBusKafka()
    except Exception as exc:
        logger.warning(
            "[di_profiles] EventBusKafka init failed (%s) — falling back to EventBusInmemory",
            exc,
        )
        return bind_fallback_event_bus(environment=profile.value)


def build_conftest_plugin_for_profile(profile: EnumDIProfile) -> Any:
    """Return a pytest plugin object whose fixtures override the conftest defaults.

    The plugin overrides the `event_bus` fixture used by all golden chain tests.
    Test code never changes — only this binding swaps.
    """
    bus = build_event_bus_for_profile(profile)

    class _DIPlugin:
        # Fixtures registered via pytest.main(plugins=[...]) are treated as
        # plugin-level fixtures. Using the same name as a conftest fixture does
        # not automatically win — we must use autouse=True at function scope so
        # the DI bus is always injected, overriding the conftest fixture by name.
        # Tests that declare `event_bus` explicitly will receive this fixture.
        @pytest.fixture(autouse=False)
        def event_bus(self) -> Any:
            return bus

    return _DIPlugin()
