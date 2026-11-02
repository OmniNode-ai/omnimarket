# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The effect handler announces and returns the report with the catalog snapshot (OMN-21597)."""

import pytest
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.container.model_onex_container import ModelONEXContainer

from omnimarket.nodes.node_package_announce_effect.announce_bindings import (
    AnnounceBindings,
)
from omnimarket.nodes.node_package_announce_effect.handlers.handler_package_announce import (
    HandlerPackageAnnounce,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    AnnounceTestBindings,
    make_overlay,
)


async def container_with(bindings: AnnounceTestBindings) -> ModelONEXContainer:
    """A container the way the runtime builds one: the bindings registered in it."""
    container = ModelONEXContainer()
    await container.service_registry.register_instance(
        interface=AnnounceBindings, instance=bindings.registry
    )
    return container


@pytest.mark.unit
async def test_handler_returns_the_report_and_the_catalog_snapshot(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    result = await HandlerPackageAnnounce(await container_with(bindings)).handle(
        make_overlay(bus_binding)
    )
    assert set(result.report.announced) == set(standard_contracts)
    assert result.report.refused == {}
    assert set(result.entries) == set(standard_contracts)
    assert {e.state for e in result.entries.values()} == {
        EnumCatalogEntryState.ADMITTED
    }


@pytest.mark.unit
async def test_an_unregistered_bus_binding_is_refused_before_anything_runs(
    bindings: AnnounceTestBindings, standard_contracts: dict[str, bytes]
) -> None:
    with pytest.raises(
        ModelOnexError, match="no bus is registered under the overlay binding"
    ):
        await HandlerPackageAnnounce(await container_with(bindings)).handle(
            make_overlay("not_registered")
        )
    assert bindings.buses == []
