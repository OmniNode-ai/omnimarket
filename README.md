# OmniMarket

OmniMarket is the canonical home for portable ONEX workflow packages and
contract-driven automation logic. It contains runnable node packages,
workflow orchestrators, adapter templates, projection helpers, and validation
utilities used by the wider OmniNode platform.

## Who Uses It

- Runtime owners use OmniMarket node packages through ONEX runtime discovery.
- Agent-surface repos use OmniMarket as the business-logic owner behind thin
  platform wrappers.
- Dashboard and observability surfaces consume events and projections emitted
  by OmniMarket nodes.
- Developers add new automation here when the behavior should be portable
  across tools or runtimes.

## What This Repo Owns

- `onex.nodes` entry points for contract-backed workflow nodes.
- Node package directories under `src/omnimarket/nodes/node_*`.
- `contract.yaml` and `metadata.yaml` files that define node interfaces,
  capabilities, dependencies, and runtime expectations.
- Handler logic for compute, reducer, effect, orchestrator, projection, and
  service nodes.
- Adapter templates for Claude Code, Codex, Cursor, and Gemini CLI.
- Golden-chain tests and metadata checks that prove node contracts remain
  runnable with an in-memory event bus.
- Shared Market primitives that prevent cross-node reach-in, such as projection,
  inference, routing, intelligence, ledger, and metadata helpers.

## What This Repo Does Not Own

- Platform-specific UX prompts, slash-command presentation, editor rules, or
  user-facing skill copy. Those belong in the wrapper repo for that platform.
- Concrete infrastructure services such as Kafka, Postgres, Docker runtime
  deployment, secrets management, or host bootstrapping. Those belong to the
  runtime/infrastructure layer.
- Core ONEX contract/runtime primitives such as `RuntimeLocal`,
  `EventBusInmemory`, envelope types, and shared validators. Those belong to
  `omnibase_core` or the shared compatibility packages.
- Governance policy and documentation evidence. Those belong to
  `onex_change_control`.
- Memory persistence semantics and storage adapters. Market may host runnable
  memory workflow nodes, but the memory domain remains owned by the memory repo.

## Install

```bash
uv sync --all-extras
```

The repository targets Python 3.12 and is developed with `uv`.

Root dependencies are intentionally broad today because the installed
distribution exposes all `onex.nodes` entry points from one package. Per-node
`metadata.yaml` dependency declarations are the forward-compatible package
boundary, but root dependency reduction should wait until node package install
isolation or lazy entry-point loading protects importability.

## Common Workflows

Run the normal non-Kafka test suite:

```bash
uv run pytest tests/ -v --tb=short -m "not kafka"
```

Run a focused node contract test:

```bash
uv run pytest tests/test_golden_chain_platform_readiness.py -v
```

Run static checks:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/omnimarket/ --strict
```

Verify runtime entry-point wiring:

```bash
uv run python scripts/ci/run_runtime_sweep.py
```

Verify node metadata dependency scope:

```bash
uv run python scripts/ci/check_node_metadata_dependencies.py
```

Run the in-memory workflow proof used by CI:

```bash
STATE_ROOT=$(mktemp -d)
uv run onex node node_golden_chain_sweep \
  --contract golden_chain_sweep_workflow.yaml \
  --backend event_bus=inmemory \
  --state-root "$STATE_ROOT" \
  --timeout 60 \
  --verbose
```

Generate a new node scaffold:

```bash
uv run python scripts/generate_node.py --name node_example --type compute
```

Generate adapter output from node contracts:

```bash
uv run python scripts/generate_adapters.py
```

## Package Model

OmniMarket uses four layers:

| Layer | Owner | Responsibility |
| --- | --- | --- |
| Skill surface | Wrapper repos | Collect user input, publish command events, render results. |
| Node unit | OmniMarket | One contract-backed execution boundary with handlers and tests. |
| Workflow package | OmniMarket | Domain-scoped composition of one or more node units. |
| Runtime | Core/Infra | Load contracts, wire transports, provide state and event buses. |

The hard boundary is simple: wrappers do not own business logic. If a platform
wrapper grows orchestration, routing, or durable behavior, that logic should move
into an OmniMarket node or workflow package.

## Node Model

Each node package should contain:

```text
src/omnimarket/nodes/node_example/
  __init__.py
  contract.yaml
  metadata.yaml
  handlers/
    __init__.py
    handler_example.py
```

The `contract.yaml` declares topics, input/output models, handler bindings, and
terminal events. The `metadata.yaml` declares package capabilities, dependency
scope, tags, package grouping, display name, and entry flags.

Every root `onex.nodes` entry point resolves to a package directory containing a
`contract.yaml`. Entry points are package roots, not factory callables.

## Current Canary Surfaces

Use these nodes as reference implementations when adding or reviewing node
packages:

| Node | Role | Why it matters |
| --- | --- | --- |
| `node_platform_readiness` | Compute | Pure readiness logic with contract-backed tests. |
| `node_aislop_sweep` | Compute | Repository analysis pattern with dry-run behavior. |
| `node_build_loop_orchestrator` | Orchestrator | Main build-loop workflow coordinator. |
| `node_loop_state_reducer` | Reducer | Pure FSM transition pattern. |
| `node_emit_daemon` | Service | Long-running event emission surface. |
| `node_projection_*` | Projection | Kafka-to-database read-model pattern. |

## Architecture Summary

- Handlers own logic; wrappers own invocation UX.
- Contracts are the source of truth for event topics, model bindings, and
  terminal events.
- Metadata is the source of truth for package capabilities and dependency scope.
- Runtime execution supports both in-memory local proof paths and full-stack
  event-bus execution when external services are configured.
- Handlers should depend on protocols and injected services for external I/O.
- Cross-node shared models belong in shared Market packages, not inside one
  node directory.
- Tests should prove the node contract with EventBusInmemory whenever possible.

## Documentation Map

- [Documentation index](docs/README.md)
- [Package model](docs/architecture/package-model.md)
- [Skill, package, and node boundaries](docs/architecture/skill-vs-package-vs-node.md)
- [Dependency boundary](docs/architecture/dependency-boundary.md)
- [Build-loop migration boundary](docs/migrations/build-loop.md)
- [Node catalog](docs/reference/node-catalog.md)
- [Node metadata reference](docs/reference/node-metadata.md)
- [Node testing](docs/node-testing.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [License](LICENSE)
