---
name: merge-sweep
description: Thin Codex skill shim for node_pr_lifecycle_orchestrator. Use for org-wide PR inventory, triage, merge, or fix sweeps.
---

# Merge Sweep

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_pr_lifecycle_orchestrator` node. The node owns PR inventory, triage,
verification, merge, and fix dispatch behavior. Do not add GitHub scripting,
queue logic, or PR classification logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `pr_lifecycle_orchestrator`
- **Request wrapper:** `scripts/run_codex_runtime_request.py`
- **Route:** native target-node contract route (the adapter selects
  the node contract command and terminal topics for this command).
- **Adapter transport command topic:** `onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`
- **Adapter transport response topic:** `onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1`
- **Compile-only:** pass `--compile-only` to validate the request and binding
  without publishing an event or starting a runtime. This is adapter preflight,
  not evidence that a target runtime executed the command.
- **Runtime evidence:** inspect `runtime_evidence.runtime_observation` and
  `runtime_evidence.adapter_dispatch_binding`; compile-only is `UNOBSERVED`
  with reason `compile_only`.
- **Evidence wire schema:** `runtime_evidence.schema_version` is
  `runtime-evidence/v2`; v2 requires `runtime_observation` and carries the
  resolved node contract under `adapter_dispatch_binding.node_contract`.
- **Binding fields:** `adapter_dispatch_binding` reports
  `adapter_command_topic`, `requested_response_topic`,
  `selected_terminal_topic`, and `terminal_selection` (`NODE_CONTRACT`,
  `DIRECT_DELEGATE_SKILL_CONTRACT`, or `EXPLICIT_RESPONSE_OVERRIDE`); its
  `node_contract` is the resolved, typed contract binding.

### Target node contract metadata

- **Backing node:** `node_pr_lifecycle_orchestrator`
- **Contract command name:** `pr_lifecycle_orchestrator`
- **Contract command topic:** `onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `repos` | Comma-separated repo slugs to filter | `""` |
| `dry_run` | Run without side effects | `false` |
| `inventory_only` | Stop after PR inventory | `false` |
| `fix_only` | Only run the fix phase | `false` |
| `merge_only` | Only run the merge phase | `false` |
| `enable_auto_rebase` | Auto-rebase stale PR branches before merge | `true` |
| `verify` | Run verification before merge | `false` |
| `verify_timeout_seconds` | Per-PR verification timeout | `30` |
| `onex_state_dir` | Optional state artifact directory override | Default `ONEX_STATE_DIR` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "pr_lifecycle_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `correlation_id`
- `run_id`
- `repos`
- `onex_state_dir`
- `dry_run`
- `inventory_only`
- `fix_only`
- `merge_only`
- `enable_auto_rebase`
- `verify`
- `verify_timeout_seconds`

Always include:

- `correlation_id`: UUIDv4, generated if the user does not supply one
- `run_id`: filesystem-safe identifier such as `merge-sweep-YYYYMMDDTHHMMSSZ`

Only include `onex_state_dir` when the user explicitly wants a non-default
artifact location.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_pr_lifecycle_orchestrator/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `pr_lifecycle_orchestrator`
- Target contract command topic: `onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the node result with PR counts and phase
outcomes: `prs_inventoried`, `prs_merged`, `prs_fixed`, and `prs_skipped`. All
decisions come from `node_pr_lifecycle_orchestrator`.
