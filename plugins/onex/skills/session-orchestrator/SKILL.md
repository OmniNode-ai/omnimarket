---
name: session-orchestrator
description: Thin Codex skill shim for node_session_orchestrator. Use to run the session health gate, queue scoring, and dispatch planning loop.
---

# Session Orchestrator

This skill is a thin Codex runtime request adapter shim over the OmniMarket
`node_session_orchestrator` node. The node owns health gating, queue scoring,
and dispatch planning. Do not add local health probes, dispatch compilation, or
ticket triage logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `session_orchestrator`
- **Request wrapper:** `scripts/run_codex_runtime_request.py`
- **Route:** generic Pattern-B adapter transport.
- **Adapter transport command topic:** `onex.cmd.omnimarket.pattern-b-dispatch.v1`
- **Adapter transport response topic:** `onex.evt.omnimarket.pattern-b-dispatch-completed.v1`
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

- **Backing node:** `node_session_orchestrator`
- **Contract command name:** `session_orchestrator`
- **Contract command topic:** `onex.cmd.omnimarket.session-orchestrator-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.session-orchestrator-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `correlation_id` | UUID v4 correlation id for the session run | Required |
| `session_id` | Explicit session id. Node auto-generates one when omitted. | Auto |
| `mode` | `interactive` or `autonomous` | `interactive` |
| `dry_run` | Produce plan and receipts without dispatching workers | `false` |
| `skip_health` | Skip Phase 1 health checks | `false` |
| `standing_orders_path` | Standing orders input path | `.onex_state/session/standing_orders.json` |
| `state_dir` | Session state directory | `.onex_state/session` |
| `phase` | Run one phase only (`1`, `2`, `3`) or `0` for full loop | `0` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "session_orchestrator" \
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
- `session_id`
- `mode`
- `dry_run`
- `skip_health`
- `standing_orders_path`
- `state_dir`
- `phase`

Generate a UUIDv4 `correlation_id` when the user does not supply one. Omit
`session_id` only when you want the node to auto-generate it.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_session_orchestrator/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `session_orchestrator`
- Target contract command topic: `onex.cmd.omnimarket.session-orchestrator-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.session-orchestrator-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the node result with `status`,
`halt_reason`, `health_report`, `dispatch_queue`, and `dispatch_receipts`. For
dry runs, report queue length and receipt count without inventing worker
execution details.
