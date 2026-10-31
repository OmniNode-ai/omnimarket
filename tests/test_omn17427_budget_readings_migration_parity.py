"""The budget readings migrations carry the bytes infra deploys."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

MIGRATIONS = (
    Path(__file__).resolve().parent.parent
    / "src/omnimarket/nodes/node_projection_delegation_budget_readings/migrations"
)

EXPECTED_SHA256 = {
    "0000_create_delegation_budget_readings.sql": (
        "835bc4e3b5e9f93c0ca0bea530e02cf616eb27c56b0397903827fc89fb83e595"
    ),
    "0001_grant_omninode_runtime_delegation_budget_readings.sql": (
        "7dec2aba11f649d51da66d6968ba78d51f2a2b2ef47cd624048c2fe1c4ea4b90"
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(EXPECTED_SHA256))
def test_migration_bytes_match_infra(name: str) -> None:
    digest = hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest()
    assert digest == EXPECTED_SHA256[name], (
        f"{name} differs from infra dev-20261031045550-7035bd384149"
    )
