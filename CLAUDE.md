# CLAUDE.md - OmniMarket

> **Python**: 3.12+ | **Package Manager**: uv | **Shared Standards**: See **`~/.claude/CLAUDE.md`** for shared development standards (Python, Git, testing, architecture principles) and infrastructure configuration.

---

## What This Repo Is

OmniMarket is the **Universal ONEX Workflow Package Registry** -- portable contract packages (`.oncp`) for deterministic skill execution. It converts platform-specific AI coding skills (currently 100+ markdown files in omniclaude) into ONEX contract packages executable via RuntimeLocal with zero infrastructure.

**Design doc**: `$OMNI_HOME/docs/plans/2026-04-05-omnimarket-design.md`

---

## Architecture

### Four Layers

| Layer | Definition | Example |
|-------|-----------|---------|
| **SkillSurface** | Platform-specific invocation wrapper (thin, no logic) | `omniclaude/plugins/onex/skills/aislop_sweep/SKILL.md` |
| **NodeUnit** | Single ONEX contract unit: `contract.yaml` + handler(s) | `node_aislop_sweep` |
| **WorkflowPackage** | Composed NodeUnits with internal event routing | `node_ticket_pipeline` |
| **MarketArtifact (.oncp)** | Installable distribution: archive with metadata, contracts, handlers, tests | `node_aislop_sweep-1.0.0.oncp` |

**Key rule:** Wrappers do not own business logic. They translate platform UX into package command events and render results back.

### Execution Modes

- **Standalone** (no infra): `uv run onex run node_aislop_sweep -- --dry-run --repos omniclaude` -- uses RuntimeLocal + EventBusInmemory
- **Full stack**: Same contract, same handlers, events route through Redpanda (see `~/.omnibase/.env` for broker address)

### Topic Naming Convention

`onex.{cmd|evt}.omnimarket.{event-name}.v{N}`

Topics are declared in each node's `contract.yaml` -- never hardcoded in handler code.

---

## Repo Invariants

- **Handlers own logic** -- nodes are thin coordination shells
- **Contracts are source of truth** -- YAML contracts define topics, inputs, outputs
- **Every node must have golden chain tests** -- EventBusInmemory, zero infra required
- **Entry points resolve to package directories** containing `contract.yaml`, not factory callables
- **`metadata.yaml` in every node** -- declares capabilities, dependencies, compatibility

---

## Quick Reference

```bash
# Setup
uv sync --all-extras

# Testing
uv run pytest tests/ -v                # All tests
uv run pytest tests/ -m unit            # Unit tests only

# Code Quality
uv run mypy src/omnimarket/ --strict    # Type checking
uv run ruff check src/ tests/           # Linting
uv run ruff format src/ tests/          # Formatting

# Run a node standalone (once RuntimeLocal wiring is complete)
uv run onex run node_aislop_sweep -- --dry-run
```

---

## Project Structure

```
src/omnimarket/
  __init__.py
  models/                              # Pydantic models (metadata schema, etc.)
  nodes/
    # === Build Loop & Orchestration ===
    node_build_loop_orchestrator/      # Main build loop: ticket dispatch, LLM routing, PR creation
    node_build_loop/                   # Build loop state machine
    node_loop_state_reducer/           # FSM reducer for build loop phases
    node_build_dispatch_effect/        # Dispatches build tasks to agents
    node_nightly_loop_controller/      # Persistent decision store for overnight sessions
    node_close_out/                    # Session close-out orchestration
    node_closeout_effect/              # Close-out side effects (merge sweep, reports)

    # === Code Quality & Review ===
    node_hostile_reviewer/             # Multi-model adversarial code review
    node_finding_aggregator_compute/   # Aggregates review findings across models
    node_local_review/                 # Local code review loop
    node_aislop_sweep/                 # AI slop detection sweep
    node_compliance_sweep/             # Handler contract compliance checks
    node_coverage_sweep/               # Test coverage sweep

    # === Ticket Pipeline ===
    node_ticket_pipeline/              # End-to-end ticket execution workflow
    node_ticket_classify_compute/      # LLM-based ticket classification
    node_rsd_fill_compute/             # RSD-driven pipeline fill from Linear

    # === Infrastructure & Verification ===
    node_platform_readiness/           # Platform readiness gate
    node_data_verification/            # Data integrity checks
    node_data_flow_sweep/              # Kafka-to-DB projection verification
    node_golden_chain_sweep/           # End-to-end golden chain validation
    node_runtime_sweep/                # Runtime registration wiring checks
    node_dashboard_sweep/              # Dashboard health verification
    node_dod_verify/                   # Definition-of-Done evidence checks
    node_verify_effect/                # Verification side effects
    node_process_watchdog/             # Stall detection for agent dispatches
    node_emit_daemon/                  # Event emission daemon (socket -> Kafka)

    # === Projections (Kafka -> DB) ===
    node_projection_baselines/         # Baseline metrics projection
    node_projection_delegation/        # Delegation event projection
    node_projection_llm_cost/          # LLM cost tracking projection
    node_projection_registration/      # Node registration projection
    node_projection_savings/           # Compute savings projection
    node_projection_session_outcome/   # Session outcome projection
    node_log_projection/               # Log event projection

    # === Operations ===
    node_merge_sweep/                  # Org-wide PR merge sweep
    node_pr_polish/                    # PR readiness loop (conflicts, reviews)

  adapters/
    claude_code/                       # SKILL.md wrapper templates
    cursor/                            # .mdc wrapper templates
    codex/                             # Codex instruction templates
tests/
  conftest.py                          # Shared EventBusInmemory fixtures
  test_golden_chain_*.py               # Golden chain tests per node
```

### Build Loop Architecture

The build loop (`node_build_loop_orchestrator`) is the primary autonomous work engine:

1. **CLASSIFYING** — LLM classifies tickets by complexity via `adapter_llm_classify.py`
2. **DISPATCHING** — Routes to cheapest capable model via `adapter_delegation_router.py`
3. **BUILDING** — Agent executes ticket work, creates PR
4. **VERIFYING** — Runs verification snapshots (dashboard sweep, data flow checks)
5. **CLOSING** — Merge sweep, session outcome projection

LLM routing uses GLM-4.5 as primary code gen backend. Model selection reads from env vars (`LLM_GLM_URL`, `LLM_GLM_API_KEY`, `LLM_GLM_MODEL_NAME`) — see `~/.omnibase/.env`.

The `node_nightly_loop_controller` reads standing orders from `$OMNI_HOME/.onex_state/nightly-loop-decisions.md` and manages persistent state across overnight sessions.

---

## Adding a New Node

1. Create `src/omnimarket/nodes/node_{name}/` with `__init__.py`, `contract.yaml`, `metadata.yaml`, `handlers/__init__.py`
2. Define topics in `contract.yaml` following `onex.{cmd|evt}.omnimarket.{name}.v{N}`
3. Write handler logic in `handlers/handler_{name}.py`
4. Add golden chain test in `tests/test_golden_chain_{name}.py`
5. Register entry point in `pyproject.toml` under `[project.entry-points."onex.nodes"]`

---

## Dependencies

- **omnibase_core** (editable, path-linked in dev): Contract system, EventBusInmemory, RuntimeLocal
- **pydantic**: Typed models
- **pyyaml**: Contract/metadata parsing
