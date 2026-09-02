<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/omninode-inline-white.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/omninode-inline-full-color.svg">
    <img alt="omninode" src="docs/assets/brand/omninode-inline-full-color.svg" width="420">
  </picture>
</p>

# OmniMarket

[![CI](https://github.com/OmniNode-ai/omnimarket/actions/workflows/ci.yml/badge.svg)](https://github.com/OmniNode-ai/omnimarket/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

OmniMarket is the portable ONEX (OmniNode eXecution) workflow package registry — the consolidation
target for all OmniNode automation logic. It ships 384 contract-backed node
entry points (as of v0.4.11) covering build loops, PR lifecycle management, sweeps, projections,
ledger, memory orchestration, session management, and diagnostics. Platform
wrappers invoke Market nodes for execution, but never own the business logic
themselves.

Every node is a self-contained package with a `contract.yaml` declaring its
handler bindings, input/output models, subscribed and published topics, FSM
transitions, and terminal event. The runtime loads those contracts to wire
event-bus subscriptions and inject handler dependencies — handlers never
hardcode topic strings or construct their own collaborators.

| Archetype | Purity | Description |
| --- | --- | --- |
| `compute` | Pure | Stateless transformation. No I/O side effects. |
| `reducer` | Pure | FSM state transition. Emits next-state events. |
| `effect` | Effectful | Performs external I/O (API calls, file writes, deployments). |
| `orchestrator` | Effectful | Composes sub-handlers via FSM. Owns in-process state. |
| `projection` | Effectful | Consumes event streams, writes to read models. |
| `service` | Effectful | Long-running daemon (event emission, health monitoring). |

## Documentation

Architecture, guides, and reference documentation for this repo live in the
[OmniNode knowledge base](https://github.com/OmniNode-ai/knowledge-base), not in
this repository:

- [Package model](https://github.com/OmniNode-ai/knowledge-base/blob/main/architecture/omnimarket-package-model.md) — layers, node package shape, contract rules
- [Skill, package, and node boundaries](https://github.com/OmniNode-ai/knowledge-base/blob/main/architecture/omnimarket-skill-package-node-boundaries.md)
- [Dependency boundary](https://github.com/OmniNode-ai/knowledge-base/blob/main/architecture/omnimarket-dependency-boundary.md)
- [Event registry](https://github.com/OmniNode-ai/knowledge-base/blob/main/architecture/omnimarket-event-registry.md)
- [Build-loop migration boundary](https://github.com/OmniNode-ai/knowledge-base/blob/main/architecture/omnimarket-build-loop-boundary.md)
- [Node catalog](https://github.com/OmniNode-ai/knowledge-base/blob/main/reference/omnimarket-node-catalog.md)
- [Node metadata reference](https://github.com/OmniNode-ai/knowledge-base/blob/main/reference/omnimarket-node-metadata.md)
- [Node testing pattern](https://github.com/OmniNode-ai/knowledge-base/blob/main/guides/omnimarket-node-testing.md) — including how to add a node to the harness
- [Skill-backing node pattern](https://github.com/OmniNode-ai/knowledge-base/blob/main/guides/omnimarket-skill-backing-node-pattern.md)

Governance policy and operator runbooks that carry real infrastructure values
live in [knowledge-base-internal](https://github.com/OmniNode-ai/knowledge-base-internal).

## Repository Layout

```text
omnimarket/
├── src/omnimarket/
│   ├── nodes/                  # All node packages (node_<name>/)
│   ├── events/                 # Shared event models (ledger, envelopes)
│   ├── models/                 # Cross-node shared Pydantic models
│   ├── protocols/              # Shared protocol interfaces
│   ├── projection/             # Projection helpers and base classes
│   ├── routing/                # Event routing and dispatch
│   ├── intelligence/           # LLM and inference abstractions
│   ├── inference/              # Model selection and endpoint routing
│   ├── classifiers/            # Shared classification logic
│   ├── cli/                    # CLI entry points
│   ├── config/                 # Configuration models and loaders
│   ├── configs/                # Configuration files and overlays
│   ├── data/                   # Static data assets
│   ├── registry/               # Node registry helpers
│   ├── enums/                  # Shared enumerations
│   ├── adapters/               # Adapter templates for external platforms
│   ├── runtime/                # Runtime version handshake utilities
│   ├── logging/                # Structured logging helpers
│   └── experiments/            # Experimental sub-projects (ADK eval, etc.)
├── tests/                      # Golden-chain and contract tests
├── scripts/
│   ├── ci/                     # CI gate scripts (runtime sweep, metadata check)
│   ├── validation/             # Additional validation scripts (leaked literals, contract overlay boundary, topic lint)
│   ├── generate_node.py        # Node scaffold generator
│   ├── generate_adapters.py    # Adapter output generator
│   └── lint_no_hardcoded_topics.py
├── docs/                       # Non-prose artifacts only: brand assets, OCC
│                               # work-tracking contracts, generated evidence
│                               # and audit data. Prose lives in the knowledge
│                               # base (see Documentation above).
├── pyproject.toml
└── CLAUDE.md
```

A node package is `src/omnimarket/nodes/node_<name>/` with `contract.yaml`,
`metadata.yaml`, and `handlers/`; orchestrators add `protocols/`, and nodes may
add `models/` and node-local `tests/`. Nodes are discovered through the
`onex.nodes` entry-point group in `pyproject.toml` — the runtime iterates those
entry points, loads each `contract.yaml`, and wires subscriptions automatically.

## Adding a Node

```bash
uv run python scripts/generate_node.py --name node_<name> --type compute
```

Then register the entry point in `pyproject.toml` under
`[project.entry-points."onex.nodes"]`, add a golden-chain test at
`tests/test_golden_chain_<name>.py` proving the contract over `EventBusInmemory`,
and run the validation gates below. The full pattern — contract fields, metadata
fields, harness requirements, and the canary nodes to copy from — is in the
knowledge base pages linked above.

## Common Commands

```bash
# Install dependencies
uv sync --all-extras

# Run tests (excluding Kafka-dependent tests)
uv run pytest tests/ -v --tb=short -m "not kafka"

# Unit tests only
uv run pytest tests/ -v -m unit

# Lint and format
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type-check
uv run mypy src/omnimarket/ --strict

# CI gates
uv run python -m omnimarket.nodes.node_runtime_sweep --import-check
uv run python scripts/ci/check_node_metadata_dependencies.py
```

Test markers: `unit` (isolated), `integration` (multi-component), `slow` (>1s),
`kafka` (requires a running broker).

## What This Repo Owns

- `onex.nodes` entry points for contract-backed workflow nodes.
- Node package directories under `src/omnimarket/nodes/node_*`.
- `contract.yaml` and `metadata.yaml` files defining node interfaces,
  capabilities, dependencies, and runtime expectations.
- Handler logic for compute, reducer, effect, orchestrator, projection, and
  service nodes.
- Adapter templates for external platform integrations.
- Golden-chain tests and metadata checks proving node contracts with an
  in-memory event bus.
- Shared Market primitives (projection, inference, routing, intelligence,
  ledger, and metadata helpers) that prevent cross-node reach-in.

## What This Repo Does Not Own

- Platform-specific UX prompts, slash-command presentation, editor rules, or
  user-facing skill copy — those belong in the wrapper repo for that platform.
- Concrete infrastructure services (Kafka, Postgres, Docker, secrets) — those
  belong to the runtime/infrastructure layer.
- Core ONEX primitives (`RuntimeLocal`, `EventBusInmemory`, envelope types,
  shared validators) — those belong to `omnibase_core` and compatibility
  packages.
- Governance policy and documentation evidence — those belong to
  `onex_change_control`.
- Memory persistence semantics and storage adapters — Market may host runnable
  memory workflow nodes, but the memory domain is owned by the memory repo.
- Prose documentation — it belongs in the knowledge base (see Documentation).

- [Contributing](.github/CONTRIBUTING.md)
- [License](LICENSE)
