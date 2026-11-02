# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The catalog built from announcements (OMN-21597, A1 AC2, AC6)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.enums.enum_announce_refusal_reason import EnumAnnounceRefusalReason
from omnibase_core.enums.enum_announced_node_kind import EnumAnnouncedNodeKind
from omnibase_core.enums.enum_announcement_operation import EnumAnnouncementOperation
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState
from omnibase_core.models.events.contract_registration.model_announced_package import (
    ModelAnnouncedPackage,
)
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)

from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    ANNOUNCER,
    OTHER_ANNOUNCER,
    AnnounceTestBindings,
    make_overlay,
)
from tests.nodes.node_package_announce_effect.helper_announce_site import contract_text


def _record(
    name: str,
    *,
    operation: EnumAnnouncementOperation,
    generation: int,
    announcer_id: UUID = ANNOUNCER,
    content_hash: str = "a" * 64,
) -> ModelNodeAnnouncement:
    return ModelNodeAnnouncement(
        entity_id=name,
        operation=operation,
        content_hash=content_hash,
        package=ModelAnnouncedPackage(distribution="alpha-pkg", version="1.0.0"),
        node_kind=EnumAnnouncedNodeKind.NODE,
        announcer_id=announcer_id,
        generation=generation,
        announced_at=datetime.now(UTC),
    )


@pytest.mark.unit
async def test_catalog_holds_the_three_names_at_their_hashes(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    snapshot = catalog.snapshot()
    assert set(snapshot) == set(standard_contracts)
    assert {entry.state for entry in snapshot.values()} == {
        EnumCatalogEntryState.ADMITTED
    }
    assert all(entry.contract_path is not None for entry in snapshot.values())


@pytest.mark.unit
async def test_reannouncing_one_with_changed_bytes_replaces_its_hash_only(
    site: Path,
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    before = catalog.snapshot()

    changed = contract_text(
        "fixture_two",
        handler_module="alpha_pkg.nodes.fixture_two.handler",
        extra_topic="onex.evt.fixture.fixture-two-extra.v1",
    )
    (site / "alpha_pkg" / "nodes" / "fixture_two" / "contract.yaml").write_bytes(
        changed
    )
    await step0.announce_again()

    after = catalog.snapshot()
    assert after["fixture_two"].content_hash != before["fixture_two"].content_hash, (
        "falsifier: a stale hash"
    )
    assert after["fixture_two"].generation > before["fixture_two"].generation
    assert after["fixture_two"].state is EnumCatalogEntryState.ADMITTED
    for untouched in ("fixture_one", "fixture_three"):
        assert after[untouched].content_hash == before[untouched].content_hash, (
            "falsifier: a changed neighbour"
        )
        assert after[untouched].state is before[untouched].state


@pytest.mark.unit
async def test_older_or_equal_generation_is_rejected_and_counted(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    held = catalog.lookup("fixture_one")
    assert held is not None
    before = catalog.snapshot()

    equal = catalog.apply(
        _record(
            "fixture_one",
            operation=EnumAnnouncementOperation.ANNOUNCE,
            generation=held.generation,
            content_hash="b" * 64,
        )
    )
    older = catalog.apply(
        _record(
            "fixture_one",
            operation=EnumAnnouncementOperation.WITHDRAW,
            generation=held.generation - 1 if held.generation > 1 else 1,
        )
    )

    assert not equal.accepted
    assert equal.refusal_reason is EnumAnnounceRefusalReason.STALE_GENERATION
    assert not older.accepted
    assert older.refusal_reason is EnumAnnounceRefusalReason.STALE_GENERATION
    assert catalog.stale_generation_rejections == 2
    assert catalog.snapshot() == before, "falsifier: a catalog change after either"


@pytest.mark.unit
async def test_withdrawal_from_a_non_authoritative_announcer_is_refused(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    before = catalog.snapshot()

    outcome = catalog.apply(
        _record(
            "fixture_one",
            operation=EnumAnnouncementOperation.WITHDRAW,
            generation=99,
            announcer_id=OTHER_ANNOUNCER,
        )
    )

    assert not outcome.accepted
    assert (
        outcome.refusal_reason is EnumAnnounceRefusalReason.NON_AUTHORITATIVE_ANNOUNCER
    )
    assert catalog.snapshot() == before, "falsifier: a catalog change after either"


@pytest.mark.unit
async def test_withdrawal_is_accepted_for_a_name_the_catalog_never_held_and_blocks_resurrection(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()

    withdrawn = catalog.apply(
        _record(
            "never_held", operation=EnumAnnouncementOperation.WITHDRAW, generation=7
        )
    )
    late = catalog.apply(
        _record(
            "never_held", operation=EnumAnnouncementOperation.ANNOUNCE, generation=3
        )
    )

    assert withdrawn.accepted
    assert withdrawn.state is EnumCatalogEntryState.WITHDRAWN
    assert not late.accepted, (
        "a delayed older announcement must not resurrect the entry"
    )
    entry = catalog.lookup("never_held")
    assert entry is not None
    assert entry.state is EnumCatalogEntryState.WITHDRAWN


@pytest.mark.unit
async def test_watch_yields_the_changes_made_after_it_starts(
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
) -> None:
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    watcher = catalog.watch()
    first = asyncio.ensure_future(anext(watcher))
    await asyncio.sleep(0)

    catalog.apply(
        _record(
            "watched",
            operation=EnumAnnouncementOperation.WITHDRAW,
            generation=1,
        )
    )
    change = await asyncio.wait_for(first, timeout=1)
    await watcher.aclose()

    assert change.previous_state is None
    assert change.entry.name == "watched"
    assert change.entry.state is EnumCatalogEntryState.WITHDRAWN
