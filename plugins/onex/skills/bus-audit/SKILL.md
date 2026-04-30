---
name: bus-audit
description: Thin Codex skill shim for node_bus_audit_compute. Use when auditing OmniNode event-bus topic registry and contract wiring health.
---

# Bus Audit

This skill is a thin Pattern B broker shim over the OmniMarket
`node_bus_audit_compute` node. The node owns event registry parsing, contract
wiring checks, and finding classification. Do not add Kafka probing, grep
fallbacks, or inline audit logic to this skill.

## Arguments

| Argument | Description | Default |
| --- | --- | --- |
| `scope` | Operator-facing audit scope | `local` |
| `registry_path` | Optional event registry YAML path | Node default |
| `contract_roots` | Contract roots or `contract.yaml` files to scan | Node default |
| `failures_only` | Return only error findings | `false` |
| `verbose` | Include informational findings | `false` |
| `skip_daemon` | Skip live daemon checks | `false` |
| `broker` | Broker hint for future live sampling | Omitted |
| `sample_count` | Requested sample count for future live sampling | `20` |
| `dry_run` | Run without side effects | `false` |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "bus_audit_compute" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For broker-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Map user inputs into a JSON payload using the same field names:

- `scope`
- `registry_path`
- `contract_roots`
- `failures_only`
- `verbose`
- `skip_daemon`
- `broker`
- `sample_count`
- `dry_run`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_bus_audit_compute/`
- Pattern B request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `bus_audit_compute`
- Runtime topic: `onex.cmd.omnimarket.bus-audit-compute.v1`
- Completion topic: `onex.evt.omnimarket.bus-audit-compute.v1`

## Output

Prefer `output_payloads[0]`. Render `status`, topic counts, contract count,
and findings grouped by severity. All audit behavior is owned by
`node_bus_audit_compute`.
