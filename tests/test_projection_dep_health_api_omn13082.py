# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13082 — dep-health findings projection exposed on the projection API (TDD).

The node_projection_dep_health reducer already writes dep_health_findings
(OMN-11042), but its contract had no ``projection_api`` stanza, so the snapshot
topic ``onex.snapshot.projection.dep-health.findings.v1`` was never served by the
projection API. These tests pin:

  1. The contract declares the canonical projection topic + table and exposes it.
  2. Live discovery (:func:`build_projection_topic_map`) registers the topic and
     maps it to the dep_health_findings table.
  3. The static materialization ratchet passes for the contract — db_io authority
     plus a node-local migration that creates the table (cold DDL proof).
  4. projection_api.columns expose the real finding columns.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.projection.discovery import (
    build_projection_topic_map,
    discover_contracts,
)
from omnimarket.projection.validation import (
    validate_projection_materialization_contracts,
)

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_dep_health/contract.yaml"
)

PROJECTION_TOPIC = "onex.snapshot.projection.dep-health.findings.v1"
TABLE = "dep_health_findings"


def _contract() -> dict[str, object]:
    return yaml.safe_load(CONTRACT_PATH.read_text())


def test_contract_declares_projection_api_topic_and_table() -> None:
    api = _contract()["projection_api"]
    assert api["expose"] is True, "projection_api.expose must be true"
    assert api["topic"] == PROJECTION_TOPIC, (
        f"projection_api.topic must be {PROJECTION_TOPIC!r}"
    )
    assert api["table"] == TABLE, f"projection_api.table must be {TABLE!r}"


def test_projection_api_table_matches_db_io_write_table() -> None:
    tables = _contract()["db_io"]["db_tables"]
    write_tables = [t["name"] for t in tables if t.get("access") == "write"]
    assert TABLE in write_tables, (
        f"contract must declare {TABLE!r} as a write table; got {write_tables!r}"
    )


def test_projection_api_exposes_finding_columns() -> None:
    required = {
        "run_id",
        "finding_type",
        "severity",
        "repo",
        "file_path",
        "symbol",
        "rule_id",
        "captured_at",
    }
    columns = set(_contract()["projection_api"]["columns"])
    missing = required - columns
    assert not missing, (
        f"projection_api.columns is missing required fields: {missing!r}"
    )


def test_live_discovery_registers_dep_health_topic() -> None:
    topic_map = build_projection_topic_map()
    assert PROJECTION_TOPIC in topic_map, (
        f"{PROJECTION_TOPIC!r} must be registered by live discovery"
    )
    cfg = topic_map[PROJECTION_TOPIC]
    assert cfg.table == TABLE
    assert cfg.source_contract == "node_projection_dep_health"


def test_materialization_ratchet_passes_for_dep_health() -> None:
    issues = validate_projection_materialization_contracts(
        discover_contracts(),
        contract_names={"node_projection_dep_health"},
    )
    assert issues == (), (
        "dep_health projection must have a materialization authority and cold "
        f"DDL proof; got issues: {[i.format() for i in issues]}"
    )
