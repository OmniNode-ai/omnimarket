# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Named bindings the node announce overlay resolves (OMN-21597).

The overlay names its bus, inventory store and distribution source; a deployment
registers a factory under each name. The announcer and the catalog see protocols
only, so changing an overlay's binding names changes the bus without changing code.
"""

from collections.abc import Callable

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.protocols.services.protocol_announce_bus import ProtocolAnnounceBus
from omnibase_core.protocols.services.protocol_announce_distribution_source import (
    ProtocolAnnounceDistributionSource,
)
from omnibase_core.protocols.services.protocol_announce_inventory_store import (
    ProtocolAnnounceInventoryStore,
)

from omnimarket.nodes.node_package_announce_effect.protocol_announce_message import (
    ProtocolAnnounceMessage,
)


def _missing(kind: str, name: str, known: list[str]) -> ModelOnexError:
    return ModelOnexError(
        f"no {kind} is registered under the overlay binding {name!r}; known: {sorted(known)}",
        error_code=EnumCoreErrorCode.VALIDATION_ERROR,
    )


class AnnounceBindings:
    """Registry of factories, keyed by the binding names an overlay uses."""

    def __init__(self) -> None:
        self._buses: dict[
            str, Callable[[], ProtocolAnnounceBus[ProtocolAnnounceMessage]]
        ] = {}
        self._stores: dict[str, Callable[[], ProtocolAnnounceInventoryStore]] = {}
        self._sources: dict[str, Callable[[], ProtocolAnnounceDistributionSource]] = {}

    def register_bus(
        self,
        name: str,
        factory: Callable[[], ProtocolAnnounceBus[ProtocolAnnounceMessage]],
    ) -> None:
        """Register a bus factory under a binding name."""
        self._buses[name] = factory

    def register_inventory_store(
        self, name: str, factory: Callable[[], ProtocolAnnounceInventoryStore]
    ) -> None:
        """Register an inventory store factory under a binding name."""
        self._stores[name] = factory

    def register_distribution_source(
        self, name: str, factory: Callable[[], ProtocolAnnounceDistributionSource]
    ) -> None:
        """Register a distribution source factory under a binding name."""
        self._sources[name] = factory

    def bus(self, name: str) -> ProtocolAnnounceBus[ProtocolAnnounceMessage]:
        """Build the bus bound to a name."""
        if name not in self._buses:
            raise _missing("bus", name, list(self._buses))
        return self._buses[name]()

    def inventory_store(self, name: str) -> ProtocolAnnounceInventoryStore:
        """Build the inventory store bound to a name."""
        if name not in self._stores:
            raise _missing("inventory store", name, list(self._stores))
        return self._stores[name]()

    def distribution_source(self, name: str) -> ProtocolAnnounceDistributionSource:
        """Build the distribution source bound to a name."""
        if name not in self._sources:
            raise _missing("distribution source", name, list(self._sources))
        return self._sources[name]()
