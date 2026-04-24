# OmniMarket ONCP Dependency Scope Audit

Date: 2026-04-24
Worktree: `/Users/jonah/Code/omni_worktrees/OMN-7343/omnimarket`

## Summary

`omnimarket` has a mixed dependency model today:

- Root `pyproject.toml` still installs some dependencies that are only used by node packages.
- `metadata.yaml` already supports per-node dependency declarations and many nodes use it.
- Root `onex.nodes` entry points still expose all nodes from the root package, so metadata-only dependencies are not yet sufficient for clean entry-point importability.
- Some nodes have no `metadata.yaml`, which prevents ONCP-level install planning from being complete.

For the intelligence migration in this workstream, migrated nodes were made self-contained by moving the small shared intelligence enums/domain/event models into `omnimarket.intelligence`. That avoids adding `omninode-intelligence` to root dependencies and avoids relying on metadata-only dependencies before ONCP install isolation exists.

## Findings

Static import audit results:

- Node directories: 135
- Node directories with `metadata.yaml`: 112
- Node directories missing `metadata.yaml`: 22
- Node imports missing matching metadata dependency declarations: 28

Root dependencies that appear node-only by import location:

- `httpx`: used by 11 node packages; no non-node imports found.
- `omnibase-infra`: used by 7 node packages; no non-node imports found.
- `omnibase-spi`: used by 1 node package; no non-node imports found.
- `omninode-memory`: used by 14 node packages; no non-node imports found.
- `psycopg2-binary`: used by 2 node packages; no non-node imports found.

Root dependencies that also have non-node usage and need more careful treatment:

- `aiokafka`
- `asyncpg`
- `omnibase-compat`
- `onex-change-control`

Additional metadata gaps found by import scan:

- `qdrant-client`, `structlog`, `confluent-kafka`, `typing-extensions`, `aiohttp`, and `radon` appear in node imports but are not consistently declared in node metadata.
- `radon` is optional in `node_quality_scoring_compute`; this should be modeled explicitly rather than silently hidden by fallback behavior.

## Recommended Work

1. Add/complete `metadata.yaml` for all node directories.
2. Add a CI check that compares node imports against `metadata.yaml.dependencies`.
3. Decide which root dependencies are true framework/runtime dependencies versus ONCP package dependencies.
4. Remove node-only dependencies from root only after entry-point importability is protected by ONCP install isolation or lazy entry-point loading.
5. Add an explicit optional-dependency representation to metadata if optional imports such as `radon` are allowed.

## Current Workstream Boundary

This audit does not remove broad root dependencies yet. That would be unsafe while root `onex.nodes` entry points can import all node packages from one installed distribution. The immediate fix is to keep the intelligence migration clean by avoiding `omninode-intelligence` imports and documenting the broader cleanup as a separate ticket.
