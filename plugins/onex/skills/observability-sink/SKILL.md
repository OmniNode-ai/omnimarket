---
name: observability-sink
description: Thin Codex skill shim for node_observability_sink_effect. Use when persisting OmniNode observability events through runtime adapters.
---

# Observability Sink

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_observability_sink_effect` node. The node owns observability persistence
through typed runtime adapters. Do not add ActionLogger calls, direct database
writes, direct Kafka producers, handler imports, or local storage fallbacks to
this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `observability_sink_effect`
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

- **Backing node:** `node_observability_sink_effect`
- **Contract command name:** `node_observability_sink_effect`
- **Contract command topic:** `onex.cmd.omnimarket.observability-sink.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.observability-persisted.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `correlation_id` | UUID correlation id for the run | Required |
| `session_id` | UUID session id | Required |
| `events` | Ordered action event payloads | Required |
| `sink_kafka` | Persist through the injected Kafka sink adapter | `true` |
| `sink_postgres` | Persist through the injected PostgreSQL sink adapter | `true` |
| `submitted_at` | ISO-8601 submission timestamp | Current UTC time |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "observability_sink_effect" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

For a side-effect-free runtime smoke, set both `sink_kafka` and `sink_postgres`
to `false`. Do not replace that with direct file, database, or broker writes.

Map user inputs into a JSON payload using the same field names:

- `correlation_id`
- `session_id`
- `events`
- `sink_kafka`
- `sink_postgres`
- `submitted_at`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_observability_sink_effect/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `observability_sink_effect`
- Target contract command topic: `onex.cmd.omnimarket.observability-sink.v1`
- Target contract terminal topic: `onex.evt.omnimarket.observability-persisted.v1`

## Output

Prefer `output_payloads[0]`. Render `persisted_event_count`,
`kafka_trace_ids`, `postgres_row_ids`, and `error`. All persistence behavior is
owned by `node_observability_sink_effect`.
