# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the live writer independently of the standalone history importer."""

from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _resolve_projection_database_target,
)
from omnibase_infra.topology.application_database import (
    SUPPORTED_TOPOLOGY_PROFILES,
    load_topology_profile,
)
from omnibase_infra.validation.application_database_domain_enforcement import (
    load_application_database_ownership_identities,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = REPO_ROOT / "src/omnimarket/nodes/node_projection_work_ledger/contract.yaml"

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("profile", sorted(SUPPORTED_TOPOLOGY_PROFILES))
def test_live_writer_resolves_with_the_released_topology(profile: str) -> None:
    """A standalone import must not prevent the event-driven writer from wiring."""
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    db_io = manifest.contracts[0].db_io
    assert db_io is not None
    target = _resolve_projection_database_target(
        db_io.db_tables, load_topology_profile(profile)
    )
    assert {table.name for table in target.tables} == {
        "work_ledger_rows",
        "work_ledger_state",
    }
    assert all(table.write_binding is not None for table in target.table_targets)


def test_history_citations_retain_authoritative_service_ownership() -> None:
    """The import table stays catalogued even though the live writer doesn't use it."""
    identities = load_application_database_ownership_identities(
        [REPO_ROOT / "scripts/application-relation-ownership.yaml"]
    )
    citations = [row for row in identities if row.name == "ledger_line_citation"]
    assert len(citations) == 1
    assert citations[0].schema == "omninode_internal"
    assert citations[0].kind.value == "table"
