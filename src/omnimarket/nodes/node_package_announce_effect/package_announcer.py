# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The package announcer: one announcement per contract of every installed package (OMN-21597).

It reads each installed distribution's own metadata, reads and hashes each contract
file, and publishes one ``ModelNodeAnnouncement`` per contract. Nothing is imported
and no container is constructed. Every contract moves through the announce lifecycle
declared in ``contract.yaml``, executed by core's FSM executor, so the
states and triggers are the contract's and not this module's.

After a restart it also withdraws every name in its persisted previous inventory
that the new inventory lacks, so a replacement process whose image lost a package
withdraws that package's contracts with no in-memory event from the old process.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from omnibase_core.enums.enum_announce_refusal_reason import EnumAnnounceRefusalReason
from omnibase_core.enums.enum_announcement_operation import EnumAnnouncementOperation
from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.contracts.subcontracts.model_fsm_state_transition import (
    ModelFSMStateTransition,
)
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)
from omnibase_core.models.nodes.package_announce.model_announce_inventory import (
    ModelAnnounceInventory,
)
from omnibase_core.models.nodes.package_announce.model_announce_inventory_entry import (
    ModelAnnounceInventoryEntry,
)
from omnibase_core.models.nodes.package_announce.model_catalog_outcome import (
    ModelCatalogOutcome,
)
from omnibase_core.protocols.services.protocol_announce_bus import ProtocolAnnounceBus
from omnibase_core.protocols.services.protocol_announce_distribution_source import (
    ProtocolAnnounceDistributionSource,
)
from omnibase_core.protocols.services.protocol_announce_inventory_store import (
    ProtocolAnnounceInventoryStore,
)
from omnibase_core.utils.util_fsm_executor import execute_transition
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates
from yaml import YAMLError

from omnimarket.nodes.node_package_announce_effect.installed_contract_index import (
    InstalledContractIndex,
)
from omnimarket.nodes.node_package_announce_effect.models.model_announce_report import (
    ModelAnnounceReport,
)
from omnimarket.nodes.node_package_announce_effect.models.model_announcer_contract import (
    ModelAnnouncerContract,
)
from omnimarket.nodes.node_package_announce_effect.models.model_installed_contract import (
    ModelInstalledContract,
)
from omnimarket.nodes.node_package_announce_effect.models.model_node_announce_overlay import (
    ModelNodeAnnounceOverlay,
)
from omnimarket.nodes.node_package_announce_effect.package_metadata_reader import (
    read_installed_contracts,
)
from omnimarket.nodes.node_package_announce_effect.protocol_announce_message import (
    ProtocolAnnounceMessage,
)
from omnimarket.nodes.node_package_announce_effect.protocol_announce_outcome_source import (
    ProtocolAnnounceOutcomeSource,
)

_RETRY_TRIGGER = "acknowledgement_timeout"


def _declared_topics(contract: dict[object, object]) -> tuple[str, ...]:
    topics: set[str] = set()
    bus = contract.get("event_bus")
    if isinstance(bus, dict):
        for key in ("publish_topics", "subscribe_topics"):
            declared = bus.get(key)
            if isinstance(declared, list):
                topics.update(t for t in declared if isinstance(t, str))
    config = contract.get("topic_config")
    if isinstance(config, dict):
        topics.update(t for t in config if isinstance(t, str))
    return tuple(sorted(topics))


def _handler_dependencies(contract: dict[object, object]) -> tuple[str, ...]:
    declared = contract.get("handler_dependencies")
    if isinstance(declared, list):
        return tuple(sorted(d for d in declared if isinstance(d, str)))
    return ()


class PackageAnnouncer:
    """Publishes the announcements of every installed package on the bound bus."""

    def __init__(
        self,
        *,
        overlay: ModelNodeAnnounceOverlay,
        contract: ModelAnnouncerContract,
        bus: ProtocolAnnounceBus[ProtocolAnnounceMessage],
        inventory_store: ProtocolAnnounceInventoryStore,
        distribution_source: ProtocolAnnounceDistributionSource,
        outcome_source: ProtocolAnnounceOutcomeSource,
        contract_index: InstalledContractIndex,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._fsm = contract.lifecycle
        self._topic = overlay.announcement_topic
        self._bus = bus
        self._store = inventory_store
        self._source = distribution_source
        self._outcomes = outcome_source
        self._index = contract_index
        self._clock = clock

    @property
    def announcement_topic(self) -> str:
        """The per-workload topic this announcer publishes on."""
        return self._topic

    async def run(self) -> ModelAnnounceReport:
        """Announce every installed contract, then withdraw what the last boot had and this one lacks."""
        saved = await self._store.load(str(self._overlay.announcer_id))
        previous = (
            ModelAnnounceInventory.model_validate_json(saved)
            if saved is not None
            else ModelAnnounceInventory()
        )
        generations = dict(previous.generations)
        installed = read_installed_contracts(
            self._source.distributions(), self._source.search_roots()
        )
        announced: list[str] = []
        refused: dict[str, EnumAnnounceRefusalReason] = {}
        published: dict[str, ModelAnnounceInventoryEntry] = {}
        for item in installed:
            await self._announce_one(item, generations, announced, refused, published)
        withdrawn: list[str] = []
        kept: dict[str, ModelAnnounceInventoryEntry] = {}
        for name, entry in previous.contracts.items():
            if name in published or name in refused:
                continue
            reason = await self._withdraw_one(name, entry, generations)
            if reason is None:
                withdrawn.append(name)
            else:
                refused[name] = reason
                kept[name] = entry
        await self._store.save(
            str(self._overlay.announcer_id),
            ModelAnnounceInventory(
                contracts={**kept, **published}, generations=generations
            ).model_dump_json(indent=2),
        )
        return ModelAnnounceReport(
            announced=tuple(announced), withdrawn=tuple(withdrawn), refused=refused
        )

    async def _advance(self, state: str, trigger: str) -> str:
        result = await execute_transition(self._fsm, state, trigger, {})
        if not result.success or result.new_state is None:
            raise ModelOnexError(
                f"announce lifecycle refused trigger {trigger!r} in state {state!r}",
                error_code=EnumCoreErrorCode.VALIDATION_ERROR,
            )
        return result.new_state

    def _source_state_of(self, trigger: str) -> str:
        for transition in self._fsm.transitions:
            if isinstance(transition, ModelFSMStateTransition) and (
                transition.trigger == trigger
            ):
                return transition.from_state
        raise ModelOnexError(
            f"announce lifecycle declares no transition for trigger {trigger!r}",
            error_code=EnumCoreErrorCode.VALIDATION_ERROR,
        )

    def _max_retries(self) -> int:
        for transition in self._fsm.transitions:
            if isinstance(transition, ModelFSMStateTransition) and (
                transition.trigger == _RETRY_TRIGGER
            ):
                return transition.max_retries
        return 0

    async def _announce_one(
        self,
        item: ModelInstalledContract,
        generations: dict[str, int],
        announced: list[str],
        refused: dict[str, EnumAnnounceRefusalReason],
        published: dict[str, ModelAnnounceInventoryEntry],
    ) -> None:
        key = f"{item.package.distribution}:{item.listing_name}"
        state = await self._advance(self._fsm.initial_state, "contract_file_read")
        path = item.contract_path
        read = (
            EnumAnnounceRefusalReason.CONTRACT_FILE_MISSING
            if path is None
            else self._read_contract(path)
        )
        if path is None or isinstance(read, EnumAnnounceRefusalReason):
            await self._advance(state, "contract_invalid")
            refused[key] = (
                read
                if isinstance(read, EnumAnnounceRefusalReason)
                else EnumAnnounceRefusalReason.CONTRACT_FILE_MISSING
            )
            return
        raw, contract, name = read
        state = await self._advance(state, "content_hash_computed")
        self._index.register(item.package, name, path)
        distribution = item.package.distribution
        generations[distribution] = generations.get(distribution, 0) + 1
        record = ModelNodeAnnouncement(
            entity_id=name,
            operation=EnumAnnouncementOperation.ANNOUNCE,
            content_hash=ModelNodeAnnouncement.canonical_content_hash(raw),
            package=item.package,
            node_kind=item.node_kind,
            declared_topics=_declared_topics(contract),
            handler_dependencies=_handler_dependencies(contract),
            announcer_id=self._overlay.announcer_id,
            generation=generations[distribution],
            announced_at=self._clock(),
        )
        published[name] = ModelAnnounceInventoryEntry(
            package=item.package,
            content_hash=record.content_hash,
            node_kind=item.node_kind,
        )
        outcome = await self._publish_and_wait(record, state)
        if outcome is not None and outcome.accepted:
            announced.append(name)
        else:
            refused[name] = (
                outcome.refusal_reason
                if outcome is not None and outcome.refusal_reason is not None
                else EnumAnnounceRefusalReason.ACKNOWLEDGEMENT_TIMEOUT
            )

    async def _withdraw_one(
        self,
        name: str,
        entry: ModelAnnounceInventoryEntry,
        generations: dict[str, int],
    ) -> EnumAnnounceRefusalReason | None:
        distribution = entry.package.distribution
        generations[distribution] = generations.get(distribution, 0) + 1
        record = ModelNodeAnnouncement(
            entity_id=name,
            operation=EnumAnnouncementOperation.WITHDRAW,
            content_hash=entry.content_hash,
            package=entry.package,
            node_kind=entry.node_kind,
            announcer_id=self._overlay.announcer_id,
            generation=generations[distribution],
            announced_at=self._clock(),
        )
        await self._advance(self._source_state_of("package_removed"), "package_removed")
        await self._bus.publish(
            self._topic, name.encode(), record.model_dump_json().encode()
        )
        outcome = await self._await_outcome(name, record.generation)
        if outcome is None:
            return EnumAnnounceRefusalReason.ACKNOWLEDGEMENT_TIMEOUT
        return None if outcome.accepted else outcome.refusal_reason

    @staticmethod
    def _read_contract(
        path: Path,
    ) -> tuple[bytes, dict[object, object], str] | EnumAnnounceRefusalReason:
        if not path.is_file():
            return EnumAnnounceRefusalReason.CONTRACT_FILE_MISSING
        try:
            raw = path.read_bytes()
            contract = load_yaml_mapping_no_duplicates(
                raw.decode("utf-8"), source=str(path)
            )
        except (OSError, ValueError, YAMLError):
            return EnumAnnounceRefusalReason.CONTRACT_UNREADABLE
        name = contract.get("name")
        if not isinstance(name, str) or not name:
            return EnumAnnounceRefusalReason.CONTRACT_NAME_MISSING
        return raw, contract, name

    async def _publish_and_wait(
        self, record: ModelNodeAnnouncement, hashed_state: str
    ) -> ModelCatalogOutcome | None:
        retries = 0
        state = hashed_state
        while True:
            state = await self._advance(state, "announcement_published")
            await self._bus.publish(
                self._topic,
                record.entity_id.encode(),
                record.model_dump_json().encode(),
            )
            outcome = await self._await_outcome(record.entity_id, record.generation)
            if outcome is not None:
                trigger = (
                    "catalog_acknowledged" if outcome.accepted else "catalog_refused"
                )
                await self._advance(state, trigger)
                return outcome
            if retries >= self._max_retries():
                return None
            retries += 1
            state = await self._advance(state, _RETRY_TRIGGER)

    async def _await_outcome(
        self, name: str, generation: int
    ) -> ModelCatalogOutcome | None:
        for attempt in range(self._overlay.acknowledgement_attempts):
            outcome = self._outcomes.outcome_for(name, generation)
            if outcome is not None:
                return outcome
            if attempt + 1 < self._overlay.acknowledgement_attempts:
                await asyncio.sleep(self._overlay.acknowledgement_poll_seconds)
        return None
