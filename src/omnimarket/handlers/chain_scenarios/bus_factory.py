# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the bus a chain scenario overlay selects.

In-memory is the default. The broker bus is provided by an entry point the overlay names,
so this package names no infrastructure symbol; the bootstrap servers come from the
environment variable the overlay names.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import cast

from omnibase_core.container.container_auto_config import local_event_bus
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
)

from omnimarket.handlers.chain_scenarios.model_chain_scenario_overlay import (
    ModelChainScenarioOverlay,
)


def create_bus(overlay: ModelChainScenarioOverlay) -> ProtocolLocalRuntimeBus:
    """The overlay's bus: an isolated in-memory bus, or the broker the overlay names."""
    overrides = overlay.backend_overrides()
    if overlay.bus == "inmemory":
        return cast(
            "ProtocolLocalRuntimeBus",
            local_event_bus(environment="local", group="chain-scenario"),
        )
    assert overlay.backend_group is not None
    assert overlay.backend_name is not None
    provided = [
        ep
        for ep in entry_points(group=overlay.backend_group)
        if ep.name == overlay.backend_name
    ]
    if not provided:
        raise ModelOnexError(
            message=f"no entry point {overlay.backend_name!r} in group {overlay.backend_group!r}; the broker overlay has no bus to build",
            error_code=EnumCoreErrorCode.CONFIGURATION_ERROR,
        )
    bus_class = provided[0].load()
    return cast(
        "ProtocolLocalRuntimeBus",
        bus_class.from_bootstrap(overrides["kafka_bootstrap"]),
    )


__all__ = ["create_bus"]
