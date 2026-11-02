# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A replacement process withdraws what the last boot had and this one lacks (OMN-21597, A1 AC5)."""

from pathlib import Path

import pytest
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState

from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    AnnounceTestBindings,
    make_overlay,
)
from tests.nodes.node_package_announce_effect.helper_announce_site import remove_package


@pytest.mark.unit
@pytest.mark.parametrize("store", ["memory", "file"])
async def test_second_boot_withdraws_each_contract_of_the_removed_package(
    site: Path,
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
    bus_binding: str,
    store: str,
) -> None:
    overlay = make_overlay(bus_binding, inventory_store_binding=store)
    first = await AnnounceStep0(overlay=overlay, bindings=bindings.registry).run()
    assert first.lookup("fixture_one") is not None

    remove_package(site, "alpha-pkg", "1.0.0")

    # A new process: a new bus and an empty catalog, the same persisted inventory.
    second_step = AnnounceStep0(overlay=overlay, bindings=bindings.registry)
    second = await second_step.run()

    assert second_step.report is not None
    assert set(second_step.report.withdrawn) == {"fixture_one", "fixture_two"}
    for removed in ("fixture_one", "fixture_two"):
        entry = second.lookup(removed)
        assert entry is not None
        assert entry.state is EnumCatalogEntryState.WITHDRAWN, (
            "falsifier: a contract of the removed package still admitted"
        )
    survivor = second.lookup("fixture_three")
    assert survivor is not None
    assert survivor.state is EnumCatalogEntryState.ADMITTED
    assert not any(
        entry.state is EnumCatalogEntryState.ADMITTED
        and entry.package.distribution == "alpha-pkg"
        for entry in second.snapshot().values()
    )


@pytest.mark.unit
async def test_a_withdrawal_the_catalog_refuses_stays_in_the_inventory(
    site: Path,
    bindings: AnnounceTestBindings,
    standard_contracts: dict[str, bytes],
) -> None:
    """With no authoritative announcer named for the package, the withdrawal is refused
    and the name stays in the inventory so the next boot tries again."""
    named = make_overlay("inmemory")
    await AnnounceStep0(overlay=named, bindings=bindings.registry).run()
    remove_package(site, "beta-pkg", "2.1.0")

    unnamed = make_overlay("inmemory", authoritative_announcers={})
    step0 = AnnounceStep0(overlay=unnamed, bindings=bindings.registry)
    catalog = await step0.run()

    assert step0.report is not None
    assert step0.report.withdrawn == ()
    assert "fixture_three" in step0.report.refused
    entry = catalog.lookup("fixture_three")
    assert entry is None or entry.state is not EnumCatalogEntryState.WITHDRAWN
    third = AnnounceStep0(overlay=named, bindings=bindings.registry)
    retried = await third.run()
    assert third.report is not None
    assert third.report.withdrawn == ("fixture_three",)
    assert retried.lookup("fixture_three") is not None
