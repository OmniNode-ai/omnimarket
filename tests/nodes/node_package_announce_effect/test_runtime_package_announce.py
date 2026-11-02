# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The runtime entry announces the interpreter's own installed packages (OMN-21597).

No fixture site: the distributions are the ones installed in this environment, so the
announcer's reading of real package metadata, real entry points and real contract
files is exercised, and this node must find itself.
"""

from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.enums.enum_announced_node_kind import EnumAnnouncedNodeKind
from omnibase_core.enums.enum_catalog_entry_state import EnumCatalogEntryState
from omnibase_core.models.events.contract_registration.model_node_announcement import (
    ModelNodeAnnouncement,
)
from omnibase_core.models.nodes.package_announce.model_node_announce_policy import (
    ModelNodeAnnouncePolicy,
)

import omnimarket.nodes.node_package_announce_effect as node_package
from omnimarket.nodes.node_package_announce_effect.models.model_node_announce_overlay import (
    ModelNodeAnnounceOverlay,
)
from omnimarket.nodes.node_package_announce_effect.runtime_package_announce import (
    BINDING_BUS_INMEMORY,
    BINDING_SOURCE_INSTALLED,
    BINDING_STORE_MEMORY,
    announce_installed_packages,
    standard_bindings,
)
from tests.nodes.node_package_announce_effect.topics import INSTALLED_TOPIC

SELF = "node_package_announce_effect"


@pytest.mark.unit
async def test_the_node_finds_itself_among_the_installed_packages(
    tmp_path: Path,
) -> None:
    announcer = UUID("00000000-0000-4000-8000-0000000000b1")
    overlay = ModelNodeAnnounceOverlay(
        announcer_id=announcer,
        announcement_topic=INSTALLED_TOPIC,
        bus_binding=BINDING_BUS_INMEMORY,
        inventory_store_binding=BINDING_STORE_MEMORY,
        distribution_source_binding=BINDING_SOURCE_INSTALLED,
        policy=ModelNodeAnnouncePolicy(
            trusted_handler_prefixes=("omnimarket.",),
            active_profiles=("effects",),
            authoritative_announcers={},
        ),
        acknowledgement_attempts=2,
        acknowledgement_poll_seconds=0.0,
    )
    result = await announce_installed_packages(
        overlay,
        standard_bindings(
            inventory_directory=tmp_path, environment="test", group="installed"
        ),
    )

    entry = result.entries.get(SELF)
    assert entry is not None, "falsifier: the node is not in its own catalog"
    assert entry.state is EnumCatalogEntryState.ADMITTED
    assert entry.node_kind is EnumAnnouncedNodeKind.NODE
    assert entry.package.distribution.replace("_", "-") == "omnimarket"
    own_contract = Path(node_package.__file__).parent / "contract.yaml"
    assert entry.content_hash == ModelNodeAnnouncement.canonical_content_hash(
        own_contract.read_bytes()
    )
    assert entry.declared_topics == ("onex.evt.platform.node-announcement.v1",)
