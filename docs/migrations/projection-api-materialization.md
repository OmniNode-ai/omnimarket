# Projection API Materialization

`projection_api.expose: true` only exposes a contract-declared topic through
the projection API. It does not create the backing table or view.

Every exposed projection topic needs a separate materialization authority:

- a node-owned migration that creates the exposed table or view;
- or `db_io.db_tables`/`metadata.yaml` ownership that declares the DDL owner
  and migration path, backed by node-local cold DDL proof.

The OMN-12980 ratchet lives in `omnimarket.projection.validation` and checks
the contract name, topic, table/view, declared materialization authority, and
cold migration proof. Missing authority is a contract error; missing table/view
DDL is a cold-runtime readiness error before a dashboard can treat the surface
as projection-backed.
