# OMN-15423 source inventory evidence

This lane classifies the application relations that can be proved from the
current `omnimarket` node-contract and node-migration corpus. It intentionally
does **not** claim that repository sources are the live catalog.

## Source projection

- 53 tables have an authoritative node `CREATE TABLE` migration, and the
  repository-owned projection runner creates `omnimarket_schema_migrations`.
- 55 tables have an application `db_io.db_tables` declaration; the additional
  `delegation_shadow_comparisons` declaration has no authoritative DDL and is
  blocked.
- The generated projection also records 10 views, 14 functions, 14 implicit
  sequences, and the `pgcrypto` extension.
- Every application table declaration uses the typed six-field shape:
  `name`, `database_ref`, `schema`, `migration`, `access`, and `role`.
- The generated JSON is a review/evidence projection. Distributed node
  contracts and owning migrations remain authoritative.

The corresponding service-owned `omninode_cloud` migration stream is declared
in `omninode_infra/db/migrations/application-relation-ownership.yaml` using the
same table shape. It covers the 16 source-created control-plane, catalog, and
migration-ledger tables in that stream.

## Fail-closed gaps

The retained read-only RDS census from 2026-07-29 reports 86 base tables and 9
views/materialized views in `omnidash_analytics`. Because its durable plan
records counts but not a complete object-name export, the 54 source-created
tables leave at least 32 live base tables unreconciled. Some source objects may
also be unapplied, making the true gap larger. A fresh authorized catalog read
is required for name-for-name parity.

The companion `omninode_infra` service manifest declares the separately owned
`node_schema_migrations` source ledger. Across the two branches, that raises
source-backed analytics coverage to at most 55 of 86 tables and reduces the
minimum unresolved gap to 31. The incompatible live `schema_migrations` ledger
remains explicitly blocked because its owning DDL is absent from current source.

The following semantic/source ambiguities remain blocked:

- `delegation_judge_verdict_events`: customer ownership is unresolved.
- `delegation_workflow_state`: producer, consumers, and sensitivity are
  unresolved.
- `event_bus_events`: retained live evidence exists, but authoritative DDL does
  not.
- `delegation_shadow_comparisons`: declared by a node contract, but no
  authoritative `CREATE TABLE` migration exists.

The source dependency scan records `tenant_id` occurrences for internal
`generation_events` and `node_service_registry`. A live collision/data scan is
still required before P2 may remove tenant stamping. The required full-day
`(datname, usename)` activity sample also remains blocked because live database
access was outside this build lane's authorization.

## Reproduction

```bash
uv run python scripts/generate_application_relation_inventory.py --check
uv run pytest tests/unit/scripts/test_application_relation_inventory.py -q
```

`OMN-15417` is the model/topology dependency that introduces the required
`database_ref` and `schema` fields. This inventory branch must not be merged
ahead of that dependency.
