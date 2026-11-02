# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One admission service, the same typed refusal on every bus (OMN-21597, A1 AC7)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.enums.enum_announce_refusal_reason import EnumAnnounceRefusalReason
from omnibase_core.enums.enum_announcement_operation import EnumAnnouncementOperation
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)
from omnibase_core.models.nodes.package_announce.model_catalog_entry import (
    ModelCatalogEntry,
)

from omnimarket.nodes.node_package_announce_effect.announce_catalog import (
    AnnounceCatalog,
)
from omnimarket.nodes.node_package_announce_effect.announce_step0 import (
    AnnounceStep0,
)
from omnimarket.nodes.node_package_announce_effect.installed_contract_index import (
    InstalledContractIndex,
)
from tests.nodes.node_package_announce_effect.helper_announce_buses import (
    ANNOUNCER,
    BUS_BINDINGS,
    AnnounceTestBindings,
    make_overlay,
)
from tests.nodes.node_package_announce_effect.helper_announce_site import (
    contract_text,
    write_package,
)


def _plant(site: Path) -> None:
    """Planted violations: a handler outside the trusted prefixes, a profile the lane
    does not own, and a good control."""
    write_package(
        site,
        "alpha-pkg",
        "1.0.0",
        {
            "control": contract_text(
                "control", handler_module="alpha_pkg.nodes.control.h"
            ),
            "planted_untrusted": contract_text(
                "planted_untrusted", handler_module="rogue_pkg.nodes.untrusted.handler"
            ),
            "planted_profile": contract_text(
                "planted_profile",
                handler_module="alpha_pkg.nodes.planted_profile.handler",
                profiles=("cloud_only",),
            ),
        },
    )


@pytest.mark.unit
async def test_untrusted_prefix_and_unowned_profile_are_refused_with_typed_reasons(
    site: Path, bindings: AnnounceTestBindings, bus_binding: str
) -> None:
    _plant(site)
    catalog = await AnnounceStep0(
        overlay=make_overlay(bus_binding), bindings=bindings.registry
    ).run()

    untrusted = catalog.lookup("planted_untrusted")
    profile = catalog.lookup("planted_profile")
    control = catalog.lookup("control")
    assert untrusted is not None
    assert profile is not None
    assert control is not None
    assert untrusted.state is EnumCatalogEntryState.REFUSED
    assert (
        untrusted.refusal_reason
        is EnumAnnounceRefusalReason.HANDLER_OUTSIDE_TRUSTED_PREFIXES
    )
    assert profile.state is EnumCatalogEntryState.REFUSED
    assert profile.refusal_reason is EnumAnnounceRefusalReason.PROFILE_NOT_OWNED
    assert control.state is EnumCatalogEntryState.ADMITTED


@pytest.mark.unit
async def test_the_refusal_reasons_are_identical_on_every_bus(
    site: Path, bindings: AnnounceTestBindings
) -> None:
    _plant(site)
    reasons = []
    for name in BUS_BINDINGS:
        catalog = await AnnounceStep0(
            overlay=make_overlay(name), bindings=bindings.registry
        ).run()
        reasons.append(
            {key: entry.refusal_reason for key, entry in catalog.snapshot().items()}
        )
    assert reasons[0] == reasons[1], "falsifier: a different reason on one"


@pytest.mark.unit
async def test_a_profile_the_lane_owns_is_admitted(
    site: Path, bindings: AnnounceTestBindings, bus_binding: str
) -> None:
    _plant(site)
    catalog = await AnnounceStep0(
        overlay=make_overlay(bus_binding, active_profiles=("main", "cloud_only")),
        bindings=bindings.registry,
    ).run()
    entry = catalog.lookup("planted_profile")
    assert entry is not None
    assert entry.state is EnumCatalogEntryState.ADMITTED


def index_of(held: ModelCatalogEntry) -> InstalledContractIndex:
    """A locator that knows where the held entry's installed file is."""
    assert held.contract_path is not None
    index = InstalledContractIndex()
    index.register(held.package, held.name, held.contract_path)
    return index


@pytest.mark.unit
async def test_a_file_changed_after_announcement_fails_the_hash_check(
    site: Path, bindings: AnnounceTestBindings, bus_binding: str
) -> None:
    """Admission reads the installed file again: bytes that no longer hash to the
    announced value are refused, not admitted."""
    write_package(
        site,
        "alpha-pkg",
        "1.0.0",
        {"drift": contract_text("drift", handler_module="alpha_pkg.nodes.drift.h")},
    )
    step0 = AnnounceStep0(overlay=make_overlay(bus_binding), bindings=bindings.registry)
    catalog = await step0.run()
    held = catalog.lookup("drift")
    assert held is not None
    assert held.contract_path is not None
    held.contract_path.write_bytes(b"name: drift\n# edited after the announcement\n")

    service = AnnounceCatalog(
        policy=make_overlay(bus_binding).policy,
        locator=index_of(held),
        group_id_suffix=ANNOUNCER,
    )
    outcome = service.apply(
        ModelNodeAnnouncement(
            entity_id="drift",
            operation=EnumAnnouncementOperation.ANNOUNCE,
            content_hash=held.content_hash,
            package=held.package,
            node_kind=held.node_kind,
            announcer_id=ANNOUNCER,
            generation=held.generation + 1,
            announced_at=datetime.now(UTC),
        )
    )
    assert not outcome.accepted
    assert outcome.refusal_reason is EnumAnnounceRefusalReason.HASH_MISMATCH
    refused = service.lookup("drift")
    assert refused is not None
    assert refused.state is EnumCatalogEntryState.REFUSED
