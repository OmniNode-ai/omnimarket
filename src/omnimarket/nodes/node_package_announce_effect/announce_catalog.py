# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The catalog service: core's pure catalog fed by the bus (OMN-21597).

The service holds one ``ModelNodeCatalog`` and replaces it with the next catalog
that ``apply`` returns for each announcement it receives. It adds what a pure value
cannot: the bus subscription, reading the installed file's bytes for admission,
the outcome the announcer waits on, and ``watch()``. The runtime reads the
catalog; it does not scan entry points.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from uuid import UUID

from omnibase_core.enums.enum_announcement_operation import EnumAnnouncementOperation
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)
from omnibase_core.models.nodes.package_announce.model_catalog_change import (
    ModelCatalogChange,
)
from omnibase_core.models.nodes.package_announce.model_catalog_entry import (
    ModelCatalogEntry,
)
from omnibase_core.models.nodes.package_announce.model_catalog_outcome import (
    ModelCatalogOutcome,
)
from omnibase_core.models.nodes.package_announce.model_node_announce_policy import (
    ModelNodeAnnouncePolicy,
)
from omnibase_core.models.nodes.package_announce.model_node_catalog import (
    ModelNodeCatalog,
)
from omnibase_core.protocols.services.protocol_announce_bus import ProtocolAnnounceBus

from omnimarket.nodes.node_package_announce_effect.protocol_announce_message import (
    ProtocolAnnounceMessage,
)
from omnimarket.nodes.node_package_announce_effect.protocol_installed_contract_locator import (
    ProtocolInstalledContractLocator,
)


class AnnounceCatalog:
    """Holds the catalog and applies announcements to it as they arrive."""

    def __init__(
        self,
        *,
        policy: ModelNodeAnnouncePolicy,
        locator: ProtocolInstalledContractLocator,
        group_id_suffix: UUID,
    ) -> None:
        self._catalog = ModelNodeCatalog(policy=policy)
        self._locator = locator
        self._group_id = f"node-catalog.{group_id_suffix}"
        self._outcomes: dict[tuple[str, int], ModelCatalogOutcome] = {}
        self._watchers: list[asyncio.Queue[ModelCatalogChange]] = []

    @property
    def stale_generation_rejections(self) -> int:
        """How many announcements were rejected for an older or equal generation."""
        return self._catalog.stale_generation_rejections

    async def attach(
        self,
        bus: ProtocolAnnounceBus[ProtocolAnnounceMessage],
        topic: str,
    ) -> Callable[[], Awaitable[None]]:
        """Subscribe to the announcement topic; the catalog fills as records arrive."""
        return await bus.subscribe(
            topic, on_message=self._on_message, group_id=self._group_id
        )

    async def _on_message(self, message: ProtocolAnnounceMessage) -> None:
        self.apply(ModelNodeAnnouncement.model_validate_json(message.value))

    def lookup(self, name: str) -> ModelCatalogEntry | None:
        """Return the entry for a name, or ``None`` when the name was never announced."""
        return self._catalog.lookup(name)

    def snapshot(self) -> dict[str, ModelCatalogEntry]:
        """Return a copy of every entry, keyed by contract name."""
        return dict(self._catalog.entries)

    async def watch(self) -> AsyncGenerator[ModelCatalogChange, None]:
        """Yield each change made after this call, in order, until the iterator is closed."""
        queue: asyncio.Queue[ModelCatalogChange] = asyncio.Queue()
        self._watchers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._watchers.remove(queue)

    def outcome_for(self, name: str, generation: int) -> ModelCatalogOutcome | None:
        """Return what the catalog did with the announcement of this name and generation."""
        return self._outcomes.get((name, generation))

    def apply(self, announcement: ModelNodeAnnouncement) -> ModelCatalogOutcome:
        """Apply one announcement, replace the held catalog and return the outcome."""
        installed_path = (
            self._locator.locate(announcement.package, announcement.entity_id)
            if announcement.operation is EnumAnnouncementOperation.ANNOUNCE
            else None
        )
        installed_bytes = (
            installed_path.read_bytes()
            if installed_path is not None and installed_path.is_file()
            else None
        )
        self._catalog, outcome, changes = self._catalog.apply(
            announcement,
            installed_bytes=installed_bytes,
            installed_path=installed_path,
        )
        for change in changes:
            for queue in self._watchers:
                queue.put_nowait(change)
        return self._record(outcome)

    def _record(self, outcome: ModelCatalogOutcome) -> ModelCatalogOutcome:
        key = (outcome.name, outcome.generation)
        known = self._outcomes.get(key)
        if known is not None and known.accepted and not outcome.accepted:
            # A redelivered record repeats an accepted one; the announcer keeps
            # reading the first answer, while the caller sees this refusal.
            return outcome
        self._outcomes[key] = outcome
        return outcome
