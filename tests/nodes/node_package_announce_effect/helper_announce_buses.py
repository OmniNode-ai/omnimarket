# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bus bindings for the node announcement tests (OMN-21597).

This is the only test module that names a concrete bus class. The tests receive a
``AnnounceBindings`` registry and an overlay; which bus a test runs on is
the overlay's ``bus_binding`` string and nothing in a test body.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.models.nodes.package_announce.model_node_announce_policy import (
    ModelNodeAnnouncePolicy,
)
from omnibase_core.protocols.services.protocol_announce_bus import ProtocolAnnounceBus

from omnimarket.nodes.node_package_announce_effect.announce_bindings import (
    AnnounceBindings,
)
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
from omnimarket.nodes.node_package_announce_effect.protocol_announce_message import (
    ProtocolAnnounceMessage,
)
from tests.nodes.node_package_announce_effect.topics import FIXTURE_TOPIC

Handler = Callable[[ModelEventMessage], Awaitable[None]]

BUS_BINDINGS = ("inmemory", "recording")


class RecordingBus:
    """A second, independent bus: delivers in order and keeps every message."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self.messages: list[ModelEventMessage] = []

    async def start(self) -> None:
        return None

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="recording", event_type=topic, timestamp=datetime.now(UTC)
            ),
        )
        self.messages.append(message)
        for handler in list(self._subscribers[topic]):
            await handler(message)

    async def subscribe(
        self, topic: str, *, on_message: Handler, group_id: str
    ) -> Callable[[], Awaitable[None]]:
        self._subscribers[topic].append(on_message)

        async def unsubscribe() -> None:
            self._subscribers[topic].remove(on_message)

        return unsubscribe


class BlackholeBus(RecordingBus):
    """Accepts every publish and delivers none, so no acknowledgement ever comes."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="blackhole", event_type=topic, timestamp=datetime.now(UTC)
            ),
        )
        self.messages.append(message)


class CountingBus:
    """Wraps whatever bus a binding built and keeps what was published through it."""

    def __init__(self, inner: ProtocolAnnounceBus[ProtocolAnnounceMessage]) -> None:
        self._inner = inner
        self.published: list[tuple[str, bytes | None, bytes]] = []

    async def start(self) -> None:
        await self._inner.start()

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, key, value))
        await self._inner.publish(topic, key, value)

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ProtocolAnnounceMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        return await self._inner.subscribe(
            topic, on_message=on_message, group_id=group_id
        )


class AnnounceTestBindings:
    """A bindings registry plus the buses it built, for assertions."""

    def __init__(self, site: Path, inventory_directory: Path) -> None:
        self.buses: list[CountingBus] = []
        self.registry = AnnounceBindings()
        self._memory_store = InventoryStoreMemory()
        for name, factory in (
            (
                "inmemory",
                lambda: EventBusInmemory(environment="test", group="announce"),
            ),
            ("recording", RecordingBus),
            ("blackhole", BlackholeBus),
        ):
            self.registry.register_bus(name, self._counting(factory))
        self.registry.register_inventory_store("memory", lambda: self._memory_store)
        self.registry.register_inventory_store(
            "file", lambda: InventoryStoreFile(inventory_directory)
        )
        self.registry.register_distribution_source(
            "site", lambda: DistributionSourceInstalled(paths=(str(site),))
        )

    def register_bus(
        self, name: str, bus: ProtocolAnnounceBus[ProtocolAnnounceMessage]
    ) -> None:
        """Bind an already-built bus (a real broker's) under an overlay binding name."""
        self.registry.register_bus(name, self._counting(lambda: bus))

    def _counting(
        self, factory: Callable[[], ProtocolAnnounceBus[ProtocolAnnounceMessage]]
    ) -> Callable[[], ProtocolAnnounceBus[ProtocolAnnounceMessage]]:
        def build() -> ProtocolAnnounceBus[ProtocolAnnounceMessage]:
            bus = CountingBus(factory())
            self.buses.append(bus)
            return bus

        return build

    @property
    def last_bus(self) -> CountingBus:
        return self.buses[-1]


ANNOUNCER = UUID("00000000-0000-4000-8000-0000000000a1")
OTHER_ANNOUNCER = UUID("00000000-0000-4000-8000-0000000000ff")


def make_overlay(
    bus_binding: str,
    *,
    inventory_store_binding: str = "memory",
    trusted_handler_prefixes: tuple[str, ...] = ("alpha_pkg.", "beta_pkg."),
    active_profiles: tuple[str, ...] = ("main",),
    authoritative_announcers: dict[str, UUID] | None = None,
    announcer_id: UUID = ANNOUNCER,
) -> ModelNodeAnnounceOverlay:
    """An overlay that differs from another only in the names it binds."""
    return ModelNodeAnnounceOverlay(
        announcer_id=announcer_id,
        announcement_topic=FIXTURE_TOPIC,
        bus_binding=bus_binding,
        inventory_store_binding=inventory_store_binding,
        distribution_source_binding="site",
        policy=ModelNodeAnnouncePolicy(
            trusted_handler_prefixes=trusted_handler_prefixes,
            active_profiles=active_profiles,
            authoritative_announcers=(
                authoritative_announcers
                if authoritative_announcers is not None
                else {"alpha-pkg": announcer_id, "beta-pkg": announcer_id}
            ),
        ),
        acknowledgement_attempts=2,
        acknowledgement_poll_seconds=0.0,
    )
