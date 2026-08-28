# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Source-only completeness proof for the OMN-15423 relation inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "generate_application_relation_inventory.py"

pytestmark = pytest.mark.unit


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "application_relation_inventory", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_inventory_is_source_derived_and_current() -> None:
    generator = _load_generator()
    assert generator.main(["--check"]) == 0


def test_node_db_table_census_requires_exact_typed_locations() -> None:
    generator = _load_generator()

    declarations, _ = generator._load_contracts()

    assert declarations
    for rows in declarations.values():
        for declaration in rows:
            assert "database" not in declaration
            assert declaration["database_ref"]
            assert declaration["schema"]


def test_node_db_table_census_rejects_seeded_legacy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_generator()
    nodes_root = tmp_path / "nodes"
    contract_dir = nodes_root / "node_legacy_projection"
    contract_dir.mkdir(parents=True)
    (contract_dir / "contract.yaml").write_text(
        """\
name: node_legacy_projection
db_io:
  db_tables:
    - name: legacy_projection
      database: omnidash_analytics
      migration: 0001_create_legacy_projection.sql
      access: write
      role: projection
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(generator, "NODES_ROOT", nodes_root)

    with pytest.raises(ValueError, match="database"):
        generator._load_contracts()


def test_named_semantic_ambiguities_remain_fail_closed() -> None:
    """The still-ambiguous relations stay blocked; the RULED one does not.

    ``delegation_judge_verdict_events`` used to be asserted here as blocked
    with ``target_schema == "unresolved"``. It was removed from this set by the
    OPERATOR RULING of 2026-08-02 (house tenant), which answered the exact
    product question OMN-15423 left open -- "customer ownership of judge
    verdicts is unresolved" -- with "this is all per tenant", OmniNode included
    as a first-class house tenant. An operator classification is the ONLY
    sanctioned way a relation leaves the fail-closed set; the other three are
    untouched and still fail closed because nothing has ruled on them.
    """
    payload = _load_generator().build_inventory()
    blocked = {item["name"] for item in payload["blocked_relations"]}
    assert {
        "delegation_workflow_state",
        "event_bus_events",
        "schema_migrations",
    } <= blocked
    assert "delegation_judge_verdict_events" not in blocked

    judge = next(
        row
        for row in payload["relations"]
        if row["name"] == "delegation_judge_verdict_events"
    )
    assert judge["target_schema"] == "tenant"
    assert judge["domain"] == "TENANT"
    assert judge["classification_status"] == "classified"


def test_migration_owner_is_distinct_from_additional_accessors() -> None:
    payload = _load_generator().build_inventory()
    capsule = next(
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "capsule_store"
    )
    assert capsule["owner_declaration"] == "node_projection_capsule_store"
    assert capsule["accessor_nodes"] == [
        "node_capsule_effectiveness_feedback_reducer",
        "node_projection_capsule_store",
    ]


def test_declared_table_without_authoritative_ddl_is_blocked() -> None:
    payload = _load_generator().build_inventory()
    shadow = next(
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "delegation_shadow_comparisons"
    )
    assert shadow["classification_status"] == "blocked"
    assert shadow["owner_declaration"] is None
    assert shadow["authoritative_sources"] == []


def test_runtime_activity_is_not_inferred_from_checked_in_dsn_keys() -> None:
    payload = _load_generator().build_inventory()
    runtime = payload["runtime_evidence"]
    assert "OMNIDASH_ANALYTICS_DB_URL" in runtime["dsn_key_provenance"]
    assert runtime["full_day_datname_usename_activity"] == {
        "status": "blocked",
        "reason": "live database access was outside this build lane's authorization",
        "credentials_captured": False,
    }


def test_retained_live_census_gap_fails_closed() -> None:
    payload = _load_generator().build_inventory()
    census = payload["retained_live_census"]

    assert payload["completion_status"] == (
        "blocked_pending_live_catalog_and_activity_evidence"
    )
    assert census["observed_base_tables"] == 86
    # 59 as of OMN-15631 (rebased onto OMN-16316/OMN-16293): 57 as of
    # OMN-16146 (node_projection_registration / projection_watermarks), +1
    # for OMN-16316's node-owned node_projection_tenant_credentials
    # /0000_create_tenant_inference_credentials.sql CREATE TABLE (the BYOK
    # credential-ref projection table) = 58, +1 for this change's new
    # node-owned node_delegation_routing_reducer
    # /0001_create_delegation_routing_tenant_overlay.sql CREATE TABLE (the
    # v1(a) per-tenant delegation routing overlay table) = 59, +2 for
    # OMN-16777's node-owned node_projection_consumer_flow
    # /0000_create_consumer_flow_windows.sql, which creates BOTH
    # consumer_flow_windows (the per-(consumer_group, topic) throughput read
    # model) and topic_produce_windows (the upstream-production evidence that
    # separates STARVED from IDLE without polling the broker) = 61.
    assert census["source_created_tables"] == 61
    # 63 as of OMN-15631 (rebased onto OMN-16316/OMN-16293): 59 as of
    # OMN-16146, +2 for OMN-16293's two omnibase_infra#2818 catalog
    # declarations (savings_injection_signals, savings_validator_catch_signals)
    # in scripts/application-relation-ownership.yaml, satisfying the OMN-15361
    # SQL ownership gate for node_savings_estimation_compute's schema-qualified
    # CREATE TABLE (that node has no omnimarket-side contract.yaml db_io
    # declaration of its own -- it lives entirely in omnibase_infra -- so
    # these are catalog-only entries, same shape as live_events/log_entries/
    # projection_watermarks above) = 61, +1 for OMN-16316's
    # tenant_inference_credentials db_io declaration = 62, +1 for this
    # change's new node-owned db_io declaration
    # (delegation_routing_tenant_overlay), required by the shadow gate
    # (application sources must not create tables with no db_io declaration)
    # = 63. OMN-15533 keeps projection_delegation_savings and
    # projection_delegation_savings_series in the relation inventory as views,
    # not db_io.db_tables entries, so they do not raise this table count.
    # +2 for OMN-16777's node_projection_consumer_flow db_io declarations
    # (consumer_flow_windows + topic_produce_windows), both required by the
    # shadow gate since its migration creates both = 65.
    assert census["source_declared_tables"] == 65
    # 27 as of OMN-15631. This figure is arithmetic, not an observation:
    # the generator computes max(0, 86 - source_created_tables), so each
    # newly source-created table (tenant_inference_credentials, then
    # delegation_routing_tenant_overlay) necessarily drops it by one from the
    # prior 29. It does NOT assert that either table exists in the live
    # catalog -- the census was observed 2026-07-29 and neither table had
    # been created then. The bound is a LOWER bound on unreconciled live
    # tables and stays honest either way; parity_status is still "blocked".
    # 25 as of OMN-16777: consumer_flow_windows and topic_produce_windows are
    # two more source-created tables, so the same arithmetic drops the bound by
    # two. Same caveat -- neither exists in the 2026-07-29 live catalog, and
    # this remains a lower bound, not a claim about the live database.
    assert census["minimum_unreconciled_live_base_tables"] == 25
    assert census["parity_status"] == "blocked"
    assert payload["runtime_evidence"]["live_catalog_parity"]["status"] == "blocked"


def test_repository_owned_migration_ledger_uses_service_manifest() -> None:
    payload = _load_generator().build_inventory()
    ledger = next(
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "omnimarket_schema_migrations"
    )

    assert ledger["domain"] == "OMNINODE_INTERNAL"
    assert ledger["owner_declaration"] == (
        "service:omnimarket_projection_migration_runner"
    )
    assert ledger["migration_root"] == "scripts"
    assert ledger["contract_sources"] == ["scripts/application-relation-ownership.yaml"]
    assert ledger["writers"] == ["service:omnimarket_projection_migration_runner"]
    assert "PRIMARY KEY (id)" in ledger["keys"]
    assert "UNIQUE (node_name, version)" in ledger["keys"]


@pytest.mark.parametrize("name", ["generation_events", "node_service_registry"])
def test_internal_tenant_column_removal_has_dependency_evidence(name: str) -> None:
    payload = _load_generator().build_inventory()
    row = next(
        item
        for item in payload["relations"]
        if item["kind"] == "table" and item["name"] == name
    )
    evidence = row["internal_tenant_column_transform"]
    assert evidence["status"] == (
        "source_dependency_inventory_complete_runtime_collision_scan_blocked"
    )
    assert evidence["source_occurrences"]
    assert evidence["runtime_collision_scan"] == (
        "blocked_live_database_access_not_authorized"
    )
