# CLAUDE.md - OmniMarket

This file gives agent-specific working guidance for this repository. The public
architecture and onboarding entrypoint is `README.md`.

## Repo Role

OmniMarket owns portable ONEX workflow packages and contract-driven automation
logic. Platform wrappers may invoke Market nodes, but wrapper instructions must
not become the long-lived owner for workflow business logic.

## Development Rules

- Use Python 3.12 and `uv`.
- Prefer repo-local patterns before introducing a new abstraction.
- Keep node logic inside `src/omnimarket/nodes/node_*`.
- Keep shared cross-node models in shared packages such as
  `omnimarket.events`, `omnimarket.intelligence`, `omnimarket.projection`,
  `omnimarket.routing`, or `omnimarket.models`.
- Do not make one node import another node's private handler or model package.
  Promote shared types instead.
- Keep event topics declared in `contract.yaml`; avoid hardcoded topic strings
  in handlers.
- Update `metadata.yaml` when a node gains dependencies, capabilities, package
  grouping, display name, or entry flags.
- Do not add public docs that point to private workspaces, ticket URLs, ticket
  identifiers, old transfer notes, or historical execution notes.

## Common Commands

```bash
uv sync --all-extras
uv run pytest tests/ -v --tb=short -m "not kafka"
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/omnimarket/ --strict
uv run python scripts/ci/run_runtime_sweep.py
uv run python scripts/ci/check_node_metadata_dependencies.py
```

## Adding A Node

1. Create `src/omnimarket/nodes/node_<name>/`.
2. Add `__init__.py`, `contract.yaml`, `metadata.yaml`, and handler modules.
3. Register the package in `[project.entry-points."onex.nodes"]`.
4. Add a golden-chain or focused contract test under `tests/`.
5. Run the runtime sweep and metadata dependency check.

## Boundary Checks

Before opening a documentation or node PR, check:

```bash
find docs -type d -empty -print
```

The command should not report empty documentation directories. Also review
changed public docs for private workspace links, ticket-system URLs, ticket
identifiers, and historical execution notes.
