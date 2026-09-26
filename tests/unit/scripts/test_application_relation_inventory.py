# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Source-only completeness proof for the OMN-15423 relation inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

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
    assert judge["target_schema"] == "public"  # OMN-17887: TENANT lives in public
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
    # OMN-17440 tranche 2 adds the third entry. `capsule_store` gained a
    # `db_io:` ownership declaration in scripts/application-relation-ownership.yaml
    # so the OMN-15361 SQL ownership gate can resolve the topology-declared
    # `omninode_runtime` TABLE grant that tranche delivers, and the generator
    # renders every manifest declaration as an accessor attributed to
    # `service:omnimarket_projection_migration_runner`. Every tranche-1 relation
    # already reads this way (`contract_registry`, `receipt_gate_rows`,
    # `gate_activity` each carry it), so this is the shape of a declared
    # relation, not a change of meaning for this one.
    #
    # The assertion this test exists to make is UNWEAKENED and in fact sharper:
    # `owner_declaration` is still exactly the migration owner
    # `node_projection_capsule_store`, and it is still distinct from the other
    # accessors -- there are now two of those rather than one.
    assert capsule["accessor_nodes"] == [
        "node_capsule_effectiveness_feedback_reducer",
        "node_projection_capsule_store",
        "service:omnimarket_projection_migration_runner",
    ]
    assert capsule["owner_declaration"] not in (
        "node_capsule_effectiveness_feedback_reducer",
        "service:omnimarket_projection_migration_runner",
    )


def test_declared_table_without_authoritative_ddl_is_blocked() -> None:
    # Ported from omnimarket#2761 (OMN-18987), which is closed as absorbed here.
    # The property is fail-closed classification: a relation DECLARED in a
    # contract's db_io but carrying no authoritative CREATE TABLE migration in
    # this repository must classify "blocked", never "classified".
    #
    # The subject was delegation_shadow_comparisons until this change landed
    # that table's authoritative CREATE, which moves it out of this class BY
    # DESIGN. projection_delegation_summary is declared in the SAME contract
    # and still has no authoritative DDL, so it carries the property forward.
    # The subject is repointed rather than the assertion inverted: asserting
    # the new "classified" state here would have deleted this coverage, and no
    # other declared-without-DDL relation would then have a fail-closed proof.
    payload = _load_generator().build_inventory()
    declared_without_ddl = next(
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "projection_delegation_summary"
    )
    assert declared_without_ddl["classification_status"] == "blocked"
    assert declared_without_ddl["owner_declaration"] is None
    assert declared_without_ddl["authoritative_sources"] == []

    # Positive control for the same property's other side: without it, a
    # generator bug that classified everything "blocked" would leave the
    # assertions above green.
    shadow = next(
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "delegation_shadow_comparisons"
    )
    assert shadow["classification_status"] == "classified"
    assert shadow["authoritative_sources"] == [
        "src/omnimarket/nodes/node_projection_delegation/migrations"
        "/0044_restore_delegation_shadow_comparisons.sql"
    ]


def test_delegation_shadow_comparisons_is_declared_from_immutable_restore_ddl() -> None:
    payload = _load_generator().build_inventory()
    shadows = [
        row
        for row in payload["relations"]
        if row["kind"] == "table" and row["name"] == "delegation_shadow_comparisons"
    ]
    assert len(shadows) == 1
    shadow = shadows[0]
    assert shadow["classification_status"] == "classified"
    # OMN-17887: `public` is both 0044's physical SQL target and the TENANT
    # domain's schema, so target and current schema agree.
    assert shadow["target_schema"] == "public"
    assert shadow["current_schema"] == ["public"]
    assert shadow["domain"] == "TENANT"
    # The node contract remains the semantic owner.  The registry declaration
    # resolves 0044's service-runner GRANT as an accessor without supplanting
    # that owner.
    assert shadow["owner_declaration"] == "node_projection_delegation"
    assert (
        "src/omnimarket/nodes/node_projection_delegation/migrations/"
        "0044_restore_delegation_shadow_comparisons.sql"
        in shadow["authoritative_sources"]
    )
    manifest = yaml.safe_load(
        (REPO_ROOT / "scripts" / "application-relation-ownership.yaml").read_text(
            encoding="utf-8"
        )
    )
    declarations = [
        relation
        for relation in manifest["relation_evidence"]
        if relation["kind"] == "table"
        and relation["name"] == "delegation_shadow_comparisons"
    ]
    assert len(declarations) == 1
    declaration = declarations[0]
    # Must equal the typed db_io declaration's schema: the ownership loader
    # pairs evidence to declaration by (name, schema), and an unpaired evidence
    # entry makes it refuse the whole manifest. OMN-17887: that schema is
    # `public`, the TENANT domain's schema.
    assert declaration["schema"] == "public"
    assert declaration["domain"] == "TENANT"
    assert declaration["owner_declaration"] == (
        "service:omnimarket_projection_migration_runner"
    )
    assert declaration["deduplication_key_columns"] == ["correlation_id"]


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
    # +1 for OMN-16180's node-owned
    # node_projection_work_events/0001_create_work_events.sql, which creates
    # omninode_internal.work_events -- the L1 work-ledger surface of the
    # OMN-16176 ladder = 62. This is the CREATE that flips the relation from
    # classification_status "blocked" (declared by omnimarket#2217's ownership
    # manifest entry, with no authoritative CREATE yet) to "classified", so
    # source_declared_tables does NOT move again here: #2217 already counted
    # this relation once and this PR names the same one.
    # +1 for OMN-16930's node-owned node_projection_tenant_registry
    # /0000_create_tenant_registry_mirror.sql, which creates
    # omninode_internal.tenant_registry_mirror -- the cross-tenant slug<->uuid
    # index that the OMN-16930 registry-resolved migration conversion reads
    # to convert legacy slug-keyed rows to canonical tenant uuid = 63.
    # +1 for OMN-17019's node-owned node_projection_open_obligations
    # /0001_create_open_obligations.sql, which creates
    # omninode_internal.open_obligations -- the materialized "what is currently
    # owed" fold over the five work.obligation.* events = 64. Unlike OMN-16180,
    # this ticket lands the ownership declaration and the node's own migration
    # in ONE omnimarket PR, so the relation goes straight to classification
    # "classified" and BOTH counts move together in this PR.
    # +1 for OMN-18768's node-owned node_projection_runner_fleet
    # /0000_create_runner_fleet_liveness.sql, which creates
    # omninode_internal.runner_fleet_liveness -- per-runner liveness for the
    # self-hosted CI fleet, the first runner/lane/fleet/host read model in the
    # repository at all = 65. Same shape as OMN-17019: the ownership
    # declaration (metadata.yaml) and the node's own migration land in ONE
    # omnimarket PR, so the relation goes straight to "classified" and BOTH
    # counts move together here.
    # +1 for OMN-18770's node-owned node_projection_runtime_error_fingerprints
    # /0000_create_runtime_error_fingerprints.sql, which creates
    # omninode_internal.runtime_error_fingerprints -- the ranked, bus-backed
    # runtime-error surface the lab observability tab reads = 66. Like
    # OMN-17019 and unlike OMN-16180, the ownership declaration and the node's
    # own migration land in ONE omnimarket PR, so the relation goes straight to
    # classification "classified" and both counts move together here. The
    # omnibase_infra VENDORING of the same migration (#3795) is a separate PR
    # for the forward-runner's sake and moves no count in this repository.
    # +1 for OMN-18769's node-owned node_projection_lab_lane_health
    # /0000_create_lab_lane_health.sql, which creates
    # omninode_internal.lab_lane_health -- the C2 per-lane fold of lane-census
    # drift, runtime health dimensions and lab-pass verdicts = 67. Unlike the
    # two entries above, this ticket SPLIT the two: omnimarket#2678 landed the
    # ownership declaration one PR earlier and this PR brings the CREATE, so
    # only source_created_tables moves here and the relation leaves
    # classification_status "blocked".
    # +1 for OMN-18900's node-owned node_projection_dod_verdict
    # /0000_create_dod_verify_runs.sql, which creates
    # omninode_internal.dod_verify_runs -- one durable row per
    # definition-of-done verification run, keyed on ticket, correlation id and
    # completion time. Unlike OMN-18769 above, this ticket does NOT split the
    # two: the ownership declaration and the CREATE land in the same change, so
    # both counts move together.
    # +1 for OMN-18887's node-owned node_delegate_skill_orchestrator
    # /0001_delegate_skill_command_claims.sql, which creates
    # omninode_internal.delegate_skill_command_claims -- the durable,
    # correlation-keyed claim that stops a redelivered delegate-skill command
    # from re-running and re-billing the inference. Same shape as OMN-18900 and
    # unlike OMN-18769: the ownership declaration and the CREATE land in one
    # change, so both counts move together. Two things beside it move nothing
    # here -- the omnibase_infra VENDORING of the same migration (#3918), for
    # the forward-runner's sake, and the OMN-19029 companion grant migration in
    # the same node lineage, which issues privileges and creates no relation.
    # +1 for OMN-18999's node-owned node_projection_prod_promotion_gate
    # /0000_create_prod_promotion_gate_decisions.sql, which creates
    # omninode_internal.prod_promotion_gate_decisions -- one durable row per
    # prod-promotion-gate evaluation, keyed on the redeploy run = 70. This one
    # moves the CREATED count ALONE, unlike OMN-18887 directly above: the
    # ownership declaration for this relation landed one pull request earlier,
    # in omnimarket#2757, because the OMN-15361 SQL ownership gate refused the
    # omnibase_infra vendor PR without it. That is the declare-then-create
    # split, and the declared count below therefore does not move here.
    # +1 for OMN-18987's immutable 0044 restore: it creates the physical
    # public table for the logical TENANT shadow-comparison projection = 71.
    # +1 for OMN-18903's node-owned node_projection_ci_attempt_outcome
    # /0000_create_ci_attempt_outcome.sql, which creates
    # omninode_internal.ci_attempt_outcome -- one row per (repository, pull
    # request, head commit, check, run attempt) with its cause code = 72. Like
    # the runtime-error entry above and unlike the lab-lane one, the ownership
    # declaration and the node's own migration land in ONE omnimarket pull
    # request, so both counts move together here. The omnibase_infra VENDORING
    # of the same migration is a separate pull request for the forward runner
    # and moves no count in this repository.
    # OMN-19550 does NOT move this count. This pull request is step 1 of the
    # same forced three-part order the OMN-18999/OMN-18769 entries above
    # describe, and carries the declaration ALONE -- the node package and its
    # own create migration (node_projection_session_content
    # /0001_create_session_content.sql) land separately in omnimarket#2905,
    # which is what moves source_created_tables. This split exists because
    # omnibase_infra#4154 (vendoring the migration ahead of #2905, per the
    # node-migration-vendor-parity-gate) failed Application Database Domain
    # Enforcement with "requires exactly one ownership declaration": the gate
    # reads this repo's own dev tip, not the #2905 PR branch, so the
    # declaration has to land on dev first.
    assert census["source_created_tables"] == 72
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
    # +1 for OMN-16180's work_events catalog declaration in
    # scripts/application-relation-ownership.yaml = 66. This is step 1 of the
    # forced three-PR sequence documented on that entry: the declaration must
    # reach omnimarket@dev BEFORE omnibase_infra can vendor the migration,
    # because the OMN-15361 ownership gate resolves declarations from service
    # manifests only and reads this file from dev. The relation is therefore
    # DECLARED here while still classification_status "blocked" ("no
    # authoritative CREATE TABLE migration found") -- source_created_tables
    # stays 61 until the node's own migration lands in step 3, which is exactly
    # what the counts below encode.
    # +1 for OMN-16930's node-owned db_io declaration
    # (tenant_registry_mirror), required by the shadow gate since its own
    # migration creates the table in the same PR (declare and create land
    # together here, unlike the cross-repo two-step above) = 67.
    # +1 for OMN-17019's open_obligations catalog declaration in
    # scripts/application-relation-ownership.yaml = 68, landing in the same PR
    # as the migration that creates it (see the source_created_tables note
    # above). The forced split OMN-16180 lived through applies to the
    # omnibase_infra VENDORING PR, not to the omnimarket PR that authors both
    # sides -- so this ticket does not repeat the two-count-move sequence.
    # OMN-16804 then adds node_projection_delegation as a read accessor for
    # that same tenant_registry_mirror relation. That widens accessor_nodes but
    # does not create a new relation or increase the declared-table relation
    # count.
    # +1 for OMN-16770's savings_correlation_finalizations catalog declaration
    # in scripts/application-relation-ownership.yaml = 69. This is step 1 of the
    # same forced cross-repo sequence OMN-16180's work_events entry documents:
    # the declaration must reach omnimarket@dev BEFORE omnibase_infra can land
    # docker/migrations/forward/nodes/node_savings_estimation_compute/
    # 0002_create_savings_correlation_finalizations.sql, because the OMN-15361
    # ownership gate resolves declarations from service manifests only and reads
    # this file from dev. The relation is therefore DECLARED here while still
    # classification_status "blocked" ("no authoritative CREATE TABLE migration
    # found") -- source_created_tables stays 64, because the CREATE lives in
    # omnibase_infra and this repository never sees it. Same shape as the two
    # OMN-16293 savings-signal entries beside it, which are blocked for the
    # identical reason and always will be.
    # +4 for OMN-18159's four delegation aggregate VIEWS, declared in
    # scripts/application-relation-ownership.yaml as kind: view so the
    # runtime can resolve tenant_projection_writer's read privilege on them
    # (migration 0039 re-groups them on tenant_id). source_created_tables
    # stays 64 -- a view is not a base table -- so the
    # max(0, 86 - source_created_tables) retained bound below is unmoved
    # by this, which is why only this one count changes = 73.
    # +1 for OMN-18768's runner_fleet_liveness db_io declaration, landing in
    # the same PR as its CREATE (see source_created_tables above) = 74.
    # +1 for OMN-18769's omninode_internal.lab_lane_health, the per-lane
    # lab health projection, declared in
    # scripts/application-relation-ownership.yaml so the OMN-15361 domain
    # gate can resolve an owner for it = 75. source_created_tables is NOT
    # moved by this entry, unlike OMN-18768's above: the declaration names
    # src/omnimarket/nodes/node_projection_lab_lane_health/migrations
    # /0000_create_lab_lane_health.sql, and that node arrives in the separate
    # projection PR, so this tree carries the declaration with no CREATE
    # beside it yet and the relation is classification_status "blocked" --
    # the same deliberate split as omnimarket#2217 above, which declared
    # work_events one PR ahead of its CREATE. The
    # max(0, 86 - source_created_tables) retained bound below is therefore
    # unmoved, which is why only this one count changes.
    # +1 for OMN-18770's node-owned db_io declaration
    # (runtime_error_fingerprints), required by the shadow gate since its own
    # migration creates the table in the same PR = 76. Its BIGSERIAL cursor
    # sequence is a sequence relation, not a base table, so it raises neither
    # of these two table counts.
    # +1 for OMN-18769's omninode_internal.lab_lane_health, the C2 per-lane
    # fold of lane-census drift, runtime health dimensions and lab-pass
    # verdicts = 76. The ownership declaration landed one PR earlier, in
    # omnimarket#2678, and this PR brings the node and its own CREATE, so
    # source_created_tables moves with it here rather than leaving the
    # relation classification_status "blocked" -- the second half of the same
    # declare-then-create split omnimarket#2217 used for work_events.
    # +1 for OMN-18900's dod_verify_runs ownership declaration. It moved this
    # count and NOT source_created_tables, because that pull request was step 1
    # of the forced three-part order and carried the declaration ALONE -- the
    # create migration it names arrives with the node package in step 3, which
    # is THIS pull request, and which moves source_created_tables below. The
    # lab_lane_health entry above records the same split from the other side.
    # +1 for OMN-18887's delegate_skill_command_claims declaration in
    # scripts/application-relation-ownership.yaml, the manifest the OMN-15361
    # SQL ownership gate actually reads. It lands in the SAME change as the
    # CREATE above rather than one pull request earlier, so this count moves
    # with source_created_tables instead of ahead of it = 78.
    # +1 for OMN-18999's prod_promotion_gate_decisions ownership declaration,
    # one durable row per prod-promotion-gate evaluation = 79. Like
    # dod_verify_runs directly above, it moves this count and NOT
    # source_created_tables: this pull request is step 1 of the same forced
    # three-part order and carries the declaration ALONE. The create migration
    # it names arrives with the node package in step 3, omnimarket#2753, which
    # is what moves source_created_tables.
    # OMN-18999 does NOT move this count, and the absence is the point. Its
    # declaration is already here: omnimarket#2757 carried it alone as step 1
    # of the forced three-part order and moved 78 -> 79 by itself. This pull
    # request brings the node package and the CREATE, which moves
    # source_created_tables above and nothing here. A reader who expects the
    # two counts to move together should read the #2757 entry directly above.
    # OMN-18693 does NOT move this count either, for the same reason as
    # OMN-18999 directly above. It restores the missing authoritative DDL
    # for a relation this node's contract already declares, so it moves
    # source_created_tables above and nothing here.
    # +1 for OMN-18903's ci_attempt_outcome ownership declaration = 80. It
    # moves with source_created_tables above rather than ahead of it, because
    # this pull request carries the node contract and its own create migration
    # together, the same shape as OMN-18887 two entries up and the opposite of
    # the two step-1 declarations beside it.
    # +1 for OMN-19550's session_content ownership declaration = 81. It moves
    # AHEAD of source_created_tables above, not with it: this pull request is
    # step 1 of the forced three-part order and carries the declaration
    # alone. The create migration it names arrives with the node package in
    # step 3, omnimarket#2905, which is what moves source_created_tables.
    assert census["source_declared_tables"] == 81
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
    # 24 as of OMN-16180: work_events is one more source-created table, so the
    # same max(0, 86 - source_created_tables) arithmetic drops the bound by one.
    # Same caveat as every entry above -- the census was observed 2026-07-29 and
    # this table did not exist then, so this remains a LOWER bound on
    # unreconciled live tables, not a claim about the live database.
    # 23 as of OMN-16930: tenant_registry_mirror is one more source-created
    # table, so the same arithmetic drops the bound by one again. Same
    # caveat -- the 2026-07-29 census predates this table, so this stays a
    # LOWER bound, not a claim about the live database.
    # 22 as of OMN-17019: open_obligations is one more source-created table, so
    # the same max(0, 86 - source_created_tables) arithmetic drops the bound by
    # one. Same caveat as every entry above -- the census was observed
    # 2026-07-29 and this table did not exist then, so this remains a LOWER
    # bound on unreconciled live tables, not a claim about the live database.
    # 21 as of OMN-18768: runner_fleet_liveness is one more source-created
    # table, so the same max(0, 86 - source_created_tables) arithmetic drops
    # the bound by one. Same caveat as every entry above -- the census was
    # observed 2026-07-29 and this table did not exist then, so this remains a
    # LOWER bound on unreconciled live tables, not a claim about the live
    # database.
    # 20 as of OMN-18770: runtime_error_fingerprints is one more source-created
    # table, so the same max(0, 86 - source_created_tables) arithmetic drops the
    # bound by one. Same caveat as every entry above -- the census was observed
    # 2026-07-29 and this table did not exist then, so this remains a LOWER
    # bound on unreconciled live tables, not a claim about the live database.
    # 19 as of OMN-18769: lab_lane_health is one more source-created table, so
    # the same max(0, 86 - source_created_tables) arithmetic drops the bound by
    # one again, from the 20 the entry above left it at. Same caveat as every
    # entry above -- the census was observed 2026-07-29 and this table did not
    # exist then, so this remains a LOWER bound on unreconciled live tables,
    # not a claim about the live database.
    # 18 as of OMN-18900: dod_verify_runs is one more source-created table,
    # so the same max(0, 86 - source_created_tables) arithmetic drops the
    # bound by one again, from the 19 the entry above left it at. Same caveat
    # as every entry above -- the census was observed 2026-07-29 and this
    # table did not exist then, so this remains a LOWER bound on unreconciled
    # live tables, not a claim about the live database.
    # 17 as of OMN-18887: delegate_skill_command_claims is one more
    # source-created table, so the same max(0, 86 - source_created_tables)
    # arithmetic drops the bound by one again, from the 18 the entry above left
    # it at. Same caveat as every entry above -- the census was observed
    # 2026-07-29 and this table did not exist then, so this remains a LOWER
    # bound on unreconciled live tables, not a claim about the live database.
    # 16 as of OMN-18999: prod_promotion_gate_decisions is one more
    # source-created table, so the same max(0, 86 - source_created_tables)
    # arithmetic drops the bound by one again, from the 17 the entry above
    # left it at. OMN-18987's 0044 adds one more source table, reducing the
    # arithmetic lower bound to 15 without claiming a fresh live observation.
    # 14 as of OMN-18903: ci_attempt_outcome is one more source-created table,
    # so the same max(0, 86 - source_created_tables) arithmetic drops the bound
    # by one again, from the 15 the entry above left it at. Same caveat as
    # every entry above -- the census was observed 2026-07-29 and this table
    # did not exist then, so this remains a LOWER bound on unreconciled live
    # tables, not a claim about the live database.
    # OMN-19550 does NOT move this bound: it is a step-1 declaration only
    # (see the source_created_tables entry above), and this arithmetic is
    # keyed on source_created_tables, which this pull request leaves at 72.
    assert census["minimum_unreconciled_live_base_tables"] == 14
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
