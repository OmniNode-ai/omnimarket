# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Runtime entry of the announce step: the standard bindings and one call (OMN-21597).

A deployment calls ``announce_installed_packages`` before it constructs its
container, with an overlay that names the standard bindings below or its own. The
in-memory bus is core's; a durable bus is registered by the deployment under its
own name, and nothing else changes.
"""

from pathlib import Path

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_package_announce_effect.announce_bindings import (
    AnnounceBindings,
)
from omnimarket.nodes.node_package_announce_effect.announce_step0 import AnnounceStep0
from omnimarket.nodes.node_package_announce_effect.distribution_source_installed import (
    DistributionSourceInstalled,
)
from omnimarket.nodes.node_package_announce_effect.inventory_store_file import (
    InventoryStoreFile,
)
from omnimarket.nodes.node_package_announce_effect.inventory_store_memory import (
    InventoryStoreMemory,
)
from omnimarket.nodes.node_package_announce_effect.models.model_node_announce_overlay import (
    ModelNodeAnnounceOverlay,
)
from omnimarket.nodes.node_package_announce_effect.models.model_package_announce_result import (
    ModelPackageAnnounceResult,
)

BINDING_BUS_INMEMORY = "inmemory"
BINDING_STORE_MEMORY = "memory"
BINDING_STORE_FILE = "file"
BINDING_SOURCE_INSTALLED = "installed"


def standard_bindings(
    *, inventory_directory: Path, environment: str, group: str
) -> AnnounceBindings:
    """Register the in-memory bus, the memory and file inventory stores, and the installed source."""
    bindings = AnnounceBindings()
    bindings.register_bus(
        BINDING_BUS_INMEMORY,
        lambda: EventBusInmemory(environment=environment, group=group),
    )
    memory_store = InventoryStoreMemory()
    bindings.register_inventory_store(BINDING_STORE_MEMORY, lambda: memory_store)
    bindings.register_inventory_store(
        BINDING_STORE_FILE, lambda: InventoryStoreFile(inventory_directory)
    )
    bindings.register_distribution_source(
        BINDING_SOURCE_INSTALLED, DistributionSourceInstalled
    )
    return bindings


async def announce_installed_packages(
    overlay: ModelNodeAnnounceOverlay, bindings: AnnounceBindings
) -> ModelPackageAnnounceResult:
    """Announce every installed package's contracts and return what the catalog holds."""
    step0 = AnnounceStep0(overlay=overlay, bindings=bindings)
    catalog = await step0.run()
    report = step0.report
    if report is None:
        raise ModelOnexError(
            "announce step finished without a report",
            error_code=EnumCoreErrorCode.INTERNAL_ERROR,
        )
    return ModelPackageAnnounceResult(report=report, entries=catalog.snapshot())
