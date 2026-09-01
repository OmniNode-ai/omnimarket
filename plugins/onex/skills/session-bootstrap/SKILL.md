---
name: session-bootstrap
description: Thin Codex skill shim for node_session_bootstrap. Use to initialize a session contract snapshot and launchd scheduler plan.
---

# Session Bootstrap

This skill is a thin Codex runtime request adapter shim over the OmniMarket
`node_session_bootstrap` node. The node owns session contract validation,
snapshot persistence, and scheduler-plan emission. Do not add timer setup
logic or fallback scheduler logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `session_bootstrap`
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

- **Backing node:** `node_session_bootstrap`
- **Contract command name:** `session_bootstrap`
- **Contract command topic:** `onex.cmd.omnimarket.session-bootstrap-start.v2`
- **Contract terminal topic:** `onex.evt.omnimarket.session-bootstrap-completed.v2`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `session_id` | UUID for the session run | Required |
| `session_label` | Human-readable session label | `<today> session` |
| `phases_expected` | Comma-separated expected phases | `build_loop,merge_sweep,platform_readiness` |
| `max_cycles` | Maximum build loop cycles (`0` = unlimited) | `0` |
| `cost_ceiling` | Advisory cost ceiling in USD | `10.0` |
| `session_mode` | Session mode: `build`, `close-out`, or `reporting` | `build` |
| `active_sprint_id` | Explicit Linear cycle id or `auto-detect` | `auto-detect` |
| `model_routing_preference` | `local-first`, `frontier-only`, or `hybrid` | `local-first` |
| `state_dir` | State output directory | `.onex_state` |
| `dry_run` | Build artifacts without mutating scheduler state | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "session_bootstrap" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Build the payload with a nested `contract` object. A minimal shape is:

```json
{
  "session_id": "<session-id>",
  "session_mode": "build",
  "active_sprint_id": "auto-detect",
  "model_routing_preference": "local-first",
  "state_dir": ".onex_state",
  "dry_run": false,
  "contract": {
    "session_id": "<same session-id>",
    "session_label": "<today> session",
    "phases_expected": ["build_loop", "merge_sweep", "platform_readiness"],
    "max_cycles": 0,
    "cost_ceiling_usd": 10.0,
    "session_mode": "build",
    "active_sprint_id": "auto-detect",
    "model_routing_preference": "local-first"
  }
}
```

Map user arguments into that shape:

- `cost_ceiling` -> `contract.cost_ceiling_usd`
- `session_label` -> `contract.session_label`
- `phases_expected` -> `contract.phases_expected` as a JSON array of strings
- `max_cycles` -> `contract.max_cycles`
- `dry_run` -> top-level `dry_run`, and mirror it into `contract.dry_run` if the user explicitly asks for that contract flag

Keep `session_id`, `session_mode`, `active_sprint_id`, and
`model_routing_preference` aligned between the top-level request and the nested
`contract`.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_session_bootstrap/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `session_bootstrap`
- Target contract command topic: `onex.cmd.omnimarket.session-bootstrap-start.v2`
- Target contract terminal topic: `onex.evt.omnimarket.session-bootstrap-completed.v2`

## Output

Prefer `output_payloads[0]`. Render the node result with `status`,
`contract_path`, `crons_registered`, and `warnings`. Treat an empty
`crons_registered` list as valid when the node emits the launchd scheduler plan
without activating any cron shim.
