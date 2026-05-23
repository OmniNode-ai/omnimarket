# Dark Consumers Diagnosis — 2026-05-23

## Symptom

Three nodes had handler classes implemented and event_bus subscriptions wired in their
`contract.yaml`, but the runtime wiring engine never created consumer groups for them.
The nodes were silently skipped at startup.

## Root Cause

`handler_routing` was absent from each contract. The runtime wiring engine uses
`handler_routing.routing_strategy` and `handler_routing.handlers[]` to bind a Kafka
consumer group to a handler class. Without this block the engine skips the node entirely,
producing no error — the node is simply invisible to the bus.

## Affected Nodes

| Node | Subscribe Topic | Handler Class |
|------|----------------|---------------|
| `node_session_phase_evaluator` | `onex.cmd.omnimarket.session-phase-evaluate.v1` | `HandlerSessionPhaseEvaluator` |
| `node_omnigate_receipt_verifier` | `onex.cmd.omnimarket.omnigate-verify-receipt.v1` | `HandlerReceiptVerifier` |
| `node_architectural_invariant_loop` | `onex.cmd.omnimarket.arch-invariant-loop-start.v1` | `NodeArchitecturalInvariantLoop` |

## Fix

Added `handler_routing` block to each contract using `operation_match` strategy,
matching the pattern established in `node_pr_lifecycle_state_reducer/contract.yaml`.

## Verification

- `uv run ruff format src/ tests/ && uv run ruff check --fix src/ tests/` — passed
- `uv run mypy src/omnimarket/ --strict` — 0 issues, 1632 files
- `uv run pytest tests/ -v -m unit` — 3046 passed, 1 skipped
- `pre-commit run --all-files` — all hooks passed (second run clean)
