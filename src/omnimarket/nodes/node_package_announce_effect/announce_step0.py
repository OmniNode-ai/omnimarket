# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Boot step 0: announce and build the catalog before any container exists (OMN-21597).

The step takes an overlay and a registry of bindings and nothing else. It imports
no handler, constructs no node, builds no dependency-injection container and opens
no connection beyond the bus the overlay names. The container is constructed after
this step returns, with the catalog as one of its injected services.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError

from omnimarket.nodes.node_package_announce_effect.announce_bindings import (
    AnnounceBindings,
)
from omnimarket.nodes.node_package_announce_effect.announce_catalog import (
    AnnounceCatalog,
)
from omnimarket.nodes.node_package_announce_effect.announcer_contract_loader import (
    load_announcer_contract,
)
from omnimarket.nodes.node_package_announce_effect.installed_contract_index import (
    InstalledContractIndex,
)
from omnimarket.nodes.node_package_announce_effect.models.model_announce_report import (
    ModelAnnounceReport,
)
from omnimarket.nodes.node_package_announce_effect.models.model_announcer_contract import (
    ModelAnnouncerContract,
)
from omnimarket.nodes.node_package_announce_effect.models.model_node_announce_overlay import (
    ModelNodeAnnounceOverlay,
)
from omnimarket.nodes.node_package_announce_effect.package_announcer import (
    PackageAnnouncer,
)


class AnnounceStep0:
    """Runs the announcer and the catalog on the bound bus, then hands back the catalog."""

    def __init__(
        self,
        *,
        overlay: ModelNodeAnnounceOverlay,
        bindings: AnnounceBindings,
        contract: ModelAnnouncerContract | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._bindings = bindings
        self._contract = contract if contract is not None else load_announcer_contract()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._report: ModelAnnounceReport | None = None
        self._announcer: PackageAnnouncer | None = None

    @property
    def report(self) -> ModelAnnounceReport | None:
        """What the announcer did, once ``run`` has finished."""
        return self._report

    async def run(self) -> AnnounceCatalog:
        """Announce every installed contract and return the catalog built from them."""
        bus = self._bindings.bus(self._overlay.bus_binding)
        await bus.start()
        index = InstalledContractIndex()
        catalog = AnnounceCatalog(
            policy=self._overlay.policy,
            locator=index,
            group_id_suffix=self._overlay.announcer_id,
        )
        self._announcer = PackageAnnouncer(
            overlay=self._overlay,
            contract=self._contract,
            bus=bus,
            inventory_store=self._bindings.inventory_store(
                self._overlay.inventory_store_binding
            ),
            distribution_source=self._bindings.distribution_source(
                self._overlay.distribution_source_binding
            ),
            outcome_source=catalog,
            contract_index=index,
            clock=self._clock,
        )
        await catalog.attach(bus, self._announcer.announcement_topic)
        self._report = await self._announcer.run()
        return catalog

    async def announce_again(self) -> ModelAnnounceReport:
        """Run the announcer again on the same bus and catalog, after a package changed."""
        if self._announcer is None:
            raise ModelOnexError(
                "announce_again requires run() to have completed first",
                error_code=EnumCoreErrorCode.VALIDATION_ERROR,
            )
        self._report = await self._announcer.run()
        return self._report
