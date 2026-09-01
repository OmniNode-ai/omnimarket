---
name: gap
description: Thin Codex skill shim for node_gap_compute. Use when running OmniNode integration gap detection, report classification, or gap-cycle preflight.
---

# Gap

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_gap_compute` node. The node owns contract/report gap detection and
classification. Do not add Linear, GitHub, grep, or fix orchestration logic to
this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `gap_compute`
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

- **Backing node:** `node_gap_compute`
- **Contract command name:** `node_gap_compute`
- **Contract command topic:** `onex.cmd.omnimarket.gap-compute-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.gap-compute-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `subcommand` | `detect`, `fix`, `cycle`, or `reconcile` | `detect` |
| `scope` | Operator-facing gap scope | `local` |
| `epic` | Optional epic identifier | Omitted |
| `report` | Existing gap report path for fix/reconcile | Omitted |
| `repo` | Limit detect to one repo name | Omitted |
| `repo_roots` | Repo roots to scan | Node default |
| `since_days` | Closed-epic lookback hint | `30` |
| `severity_threshold` | `WARNING` or `CRITICAL` | `WARNING` |
| `max_findings` | Maximum deterministic findings | `200` |
| `max_best_effort` | Maximum best-effort findings | `50` |
| `max_iterations` | Cycle iteration cap | `3` |
| `output` | `json` or `md` | `json` |
| `dry_run` | Run without side effects | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "gap_compute" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `subcommand`
- `scope`
- `epic`
- `report`
- `repo`
- `repo_roots`
- `since_days`
- `severity_threshold`
- `max_findings`
- `max_best_effort`
- `max_iterations`
- `output`
- `dry_run`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_gap_compute/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `gap_compute`
- Target contract command topic: `onex.cmd.omnimarket.gap-compute-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.gap-compute-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `status`, scanned contract count,
findings grouped by category and severity, skipped probes, and report
classification counts when present. All gap behavior is owned by
`node_gap_compute`.
