# OmniMarket ONCP Dependency Scope Audit

Date: 2026-04-24
Worktree: `omni_worktrees/OMN-7343/omnimarket`

## Summary

`omnimarket` has a mixed dependency model today:

- Root `pyproject.toml` still installs some dependencies that are only used by node packages.
- `metadata.yaml` already supports per-node dependency declarations and many nodes use it.
- Root `onex.nodes` entry points still expose all nodes from the root package, so metadata-only dependencies are not yet sufficient for clean entry-point importability.
- Some nodes have no `metadata.yaml`, which prevents ONCP-level install planning from being complete.

For the intelligence migration in this workstream, migrated nodes were made self-contained by moving the small shared intelligence enums/domain/event models into `omnimarket.intelligence`. That avoids adding `omninode-intelligence` to root dependencies and avoids relying on metadata-only dependencies before ONCP install isolation exists.

## Findings

Static import audit results:

- Node directories: 134
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
- `radon` was treated as optional in `node_quality_scoring_compute`; OMN-9584 makes it an explicit dependency and removes the silent AST fallback from runtime scoring.

## Recommended Work

1. Add/complete `metadata.yaml` for all node directories.
2. Add a CI check that compares node imports against `metadata.yaml.dependencies`.
3. Decide which root dependencies are true framework/runtime dependencies versus ONCP package dependencies.
4. Remove node-only dependencies from root only after entry-point importability is protected by ONCP install isolation or lazy entry-point loading.
5. Add an explicit optional-dependency representation only if optional imports are intentionally supported by a future ONCP package model.

## Current Workstream Boundary

This audit does not remove broad root dependencies yet. That would be unsafe while root `onex.nodes` entry points can import all node packages from one installed distribution. The immediate fix is to keep the intelligence migration clean by avoiding `omninode-intelligence` imports and documenting the broader cleanup as a separate ticket.

## OMN-9584 Enforcement Update

OMN-9584 adds `scripts/ci/check_node_metadata_dependencies.py` and wires it into CI. The check enforces:

- every direct `src/omnimarket/nodes/node_*` package has `metadata.yaml`
- node-owned external imports are declared in `metadata.yaml.dependencies`
- imports from node tests are excluded from runtime dependency scope
- distribution names are normalized, for example `qdrant_client` -> `qdrant-client` and `omnimemory` -> `omninode-memory`

The current shared/runtime allowlist is intentionally small:

- `omnibase_core`
- `packaging`
- `pydantic`
- `python-dateutil`
- `pyyaml`

All other external imports are treated as node-owned unless the root/runtime classification is explicitly changed. This PR still does not remove node-only packages from root `pyproject.toml`; removal remains blocked until ONCP install isolation or lazy entry-point loading makes metadata-scoped installs safe. `radon` is temporarily present in root dependencies because the current root distribution still exposes all `onex.nodes` entry points.
