---
name: aislop-sweep
description: Thin Codex skill shim for node_aislop_sweep. Use when scanning OmniNode repos for AI-generated quality anti-patterns or AI slop.
---

# AI Slop Sweep

This skill is a thin Codex runtime request adapter shim over the OmniMarket
`node_aislop_sweep` node. Collect arguments, dispatch the node, and render the
node result. Do not add scan logic, grep fallbacks, ticket logic, or
remediation logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `aislop_sweep`
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

- **Backing node:** `node_aislop_sweep`
- **Contract command name:** `aislop_sweep`
- **Contract command topic:** `onex.cmd.omnimarket.aislop-sweep-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.aislop-sweep-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `target_dirs` | Absolute repo paths to scan | Required |
| `checks` | Optional list of check categories | All checks |
| `--dry-run` | Report findings without side effects | `false` |
| `severity_threshold` | Minimum severity to report | `WARNING` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "aislop_sweep" \
  --payload '<json-payload>' \
  --timeout-ms 120000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload:

- `target_dirs` -> absolute path list in `target_dirs`
- `checks` -> category list in `checks`
- `dry_run=true` -> `dry_run: true`
- `severity_threshold` -> `severity_threshold`

If the user supplies repo slugs instead of absolute paths, resolve them under
`$OMNI_HOME` before dispatch and place the resulting absolute paths in
`target_dirs`.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_aislop_sweep/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `aislop_sweep`
- Target contract command topic: `onex.cmd.omnimarket.aislop-sweep-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.aislop-sweep-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the findings summary grouped by severity
and repo, with counts and file references when present. All finding detection
is owned by `node_aislop_sweep`.
