# OmniMarket

OmniMarket is the portable ONEX workflow package registry — the consolidation
target for all OmniNode automation logic. It ships ~135 contract-backed node
entry points covering build loops, PR lifecycle management, sweeps, projections,
ledger, memory orchestration, session management, and diagnostics. Platform
wrappers invoke Market nodes for execution, but never own the business logic
themselves.

## Architecture Overview

OmniMarket follows a **contract-driven, event-sourced** architecture. Every node
is a self-contained package with a `contract.yaml` that declares its handler
bindings, input/output models, subscribed and published event topics, FSM
transitions (for orchestrators), and terminal events. The runtime loads these
contracts to wire event-bus subscriptions, inject handler dependencies, and
enforce timeout and idempotency guarantees.

### Node archetypes

Each node has an archetype that determines its execution semantics:

| Archetype | Purity | Description |
| --- | --- | --- |
| `compute` | Pure | Stateless transformation. No I/O side effects. |
| `reducer` | Pure | FSM state transition. Emits next-state events. |
| `effect` | Effectful | Performs external I/O (API calls, file writes, deployments). |
| `orchestrator` | Effectful | Composes sub-handlers via FSM. Owns in-process state. |
| `projection` | Effectful | Consumes event streams, writes to read models. |
| `service` | Effectful | Long-running daemon (event emission, health monitoring). |

### Event bus integration

Nodes declare `subscribe_topics` and `publish_topics` in their contract. Topic
names follow the convention `onex.{cmd\|evt}.{service}.{event-name}.v{N}`. The
runtime wires subscriptions automatically — handlers never hardcode topic
strings.

### Protocol-based dependency injection

Orchestrators declare `sub_handler_dependencies` in their contract, binding each
sub-handler slot to a protocol interface with a default implementation. This
allows the runtime to swap implementations for testing or platform-specific
behavior without modifying handler code.

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
│   ├── enums/                  # Shared enumerations
│   ├── adapters/               # Adapter templates for external platforms
│   ├── runtime/                # Runtime version handshake utilities
│   ├── logging/                # Structured logging helpers
│   └── experiments/            # Experimental sub-projects (ADK eval, etc.)
├── tests/                      # Golden-chain and contract tests
├── scripts/
│   ├── ci/                     # CI gate scripts (runtime sweep, metadata check)
│   ├── generate_node.py        # Node scaffold generator
│   ├── generate_adapters.py    # Adapter output generator
│   └── lint_no_hardcoded_topics.py
├── docs/
│   ├── architecture/           # Package model, boundaries, dependency scope
│   ├── reference/              # Node catalog, metadata reference
│   └── migrations/             # Migration boundary docs
├── pyproject.toml
└── CLAUDE.md
```

## Node Structure

Each node lives in `src/omnimarket/nodes/node_<name>/` and contains:

```text
node_<name>/
├── __init__.py           # Package root (entry-point target)
├── contract.yaml         # Handler bindings, topics, FSM, terminal events
├── metadata.yaml         # Capabilities, dependencies, tags, display name
├── handlers/
│   ├── __init__.py
│   └── handler_<name>.py # Core handler logic
├── models/               # Pydantic request/result models (optional)
├── protocols/            # Sub-handler protocol interfaces (orchestrators)
└── tests/                # Node-local contract tests (optional)
```

### contract.yaml

Declares the node's handler class, input/output models, event-bus topic
subscriptions, FSM state machine (for orchestrators), sub-handler dependencies
(via protocol interfaces), descriptor metadata (purity, timeout, idempotency),
and terminal event.

### metadata.yaml

Declares capabilities (`standalone`, `full_runtime`, `requires_network`, etc.),
dependency scope, package grouping (`pack`), display name, node role, tags, and
compatibility constraints. The CI metadata-dependency check validates that
declared dependencies match what the handler actually imports.

## Adding a New Node

1. **Create the directory**:

   ```bash
   mkdir -p src/omnimarket/nodes/node_<name>/handlers
   ```

2. **Write `contract.yaml`** — declare the handler module/class, input model,
   event topics, descriptor (archetype, purity, timeout), and terminal event.
   Follow an existing node of the same archetype as a template (see
   [Canary Surfaces](#canary-surfaces) below).

3. **Write `metadata.yaml`** — declare capabilities, dependency list, tags,
   `pack` grouping, display name, and node role.

4. **Implement the handler** — add `__init__.py` files and a handler module
   under `handlers/`. For orchestrators, also add `protocols/` with sub-handler
   interfaces.

5. **Register the entry point** in `pyproject.toml`:

   ```toml
   [project.entry-points."onex.nodes"]
   node_<name> = "omnimarket.nodes.node_<name>"
   ```

6. **Add a golden-chain test** under `tests/test_golden_chain_<name>.py`. The
   test should prove the node contract using `EventBusInmemory` — instantiate
   the handler, emit the start event, and assert the terminal event is
   produced with the expected result shape.

7. **Run validation gates**:

   ```bash
   uv run python scripts/ci/run_runtime_sweep.py
   uv run python scripts/ci/check_node_metadata_dependencies.py
   ```

Or use the scaffold generator:

```bash
uv run python scripts/generate_node.py --name node_<name> --type compute
```

## Entry Points and Node Discovery

All nodes are registered under the `onex.nodes` entry-point group in
`pyproject.toml`. The entry point maps a node name to its package directory:

```toml
[project.entry-points."onex.nodes"]
node_platform_readiness = "omnimarket.nodes.node_platform_readiness"
```

The runtime discovers nodes by iterating `onex.nodes` entry points, loading each
package's `contract.yaml`, and wiring event-bus subscriptions automatically.

There is also a single `onex.node_package` entry point that registers the
entire omnimarket package as a node source:

```toml
[project.entry-points."onex.node_package"]
omnimarket = "omnimarket.nodes"
```

A CLI script is available for the integration test runner:

```toml
[project.scripts]
onex-integration-test-runner = "omnimarket.nodes.node_integration_test_runner.cli:main"
```

### Canary Surfaces

Use these nodes as reference implementations when adding or reviewing node
packages:

| Node | Archetype | Description |
| --- | --- | --- |
| `node_platform_readiness` | Compute | Pure readiness logic with contract-backed tests. |
| `node_aislop_sweep` | Compute | Repository analysis with dry-run behavior. |
| `node_build_loop_orchestrator` | Orchestrator | FSM-based workflow coordinator with protocol injection. |
| `node_loop_state_reducer` | Reducer | Pure FSM state transition pattern. |
| `node_emit_daemon` | Service | Long-running event emission surface. |
| `node_projection_session_outcome` | Projection | Event-stream to read-model pattern. |

## Testing

OmniMarket uses a **golden-chain** testing pattern: each node has a dedicated
test file (`tests/test_golden_chain_<name>.py`) that proves the full
contract — handler instantiation, event emission, and result model shape —
using an in-memory event bus with no external dependencies.

### Test markers

| Marker | Purpose |
| --- | --- |
| `unit` | Isolated component tests (no network, no database) |
| `integration` | Multi-component tests |
| `slow` | Tests taking >1s |
| `kafka` | Tests requiring a running Kafka broker |

### Running tests

```bash
# Standard suite (excludes Kafka-dependent tests)
uv run pytest tests/ -v --tb=short -m "not kafka"

# Unit tests only
uv run pytest tests/ -v -m unit

# Single golden-chain test
uv run pytest tests/test_golden_chain_platform_readiness.py -v

# Skill-to-node dispatch parity
uv run pytest tests/test_skill_dispatch.py -v -m unit
```

### Node testing requirements

Every node should satisfy:

1. **Standalone execution**: `python -m omnimarket.nodes.<node_name> --dry-run`
   exits 0 or 1 and writes valid JSON to stdout.
2. **Schema parity**: JSON output is parseable as the node's result model.
3. **Handler parity**: direct `handler.handle(request)` and subprocess
   invocation produce structurally equivalent output.

## Common Commands

```bash
# Install dependencies
uv sync --all-extras

# Run tests (excluding Kafka)
uv run pytest tests/ -v --tb=short -m "not kafka"

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Type-check
uv run mypy src/omnimarket/ --strict

# CI gates
uv run python scripts/ci/run_runtime_sweep.py
uv run python scripts/ci/check_node_metadata_dependencies.py
```

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

## Documentation

- [Documentation index](docs/README.md)
- [Package model](docs/architecture/package-model.md)
- [Skill, package, and node boundaries](docs/architecture/skill-vs-package-vs-node.md)
- [Dependency boundary](docs/architecture/dependency-boundary.md)
- [Build-loop migration](docs/migrations/build-loop.md)
- [Node catalog](docs/reference/node-catalog.md)
- [Node metadata reference](docs/reference/node-metadata.md)
- [Node testing](docs/node-testing.md)
- [Contributing](CONTRIBUTING.md)
- [License](LICENSE)
