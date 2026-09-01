---
name: ticket-pipeline
description: Thin Codex skill shim for node_ticket_pipeline. Use to run the bounded per-ticket pipeline slice through the OmniMarket runtime adapter.
---

# Ticket Pipeline

This skill is a thin Codex runtime adapter shim over the OmniMarket
`node_ticket_pipeline` node. The node owns pre-flight checks, compile-only
implementation dispatch, phase state, and bounded stop behavior for unwired
side-effect phases. Do not add Linear fetches, agent dispatch, PR creation,
test loops, CI polling, or merge logic to this skill.

## Arguments

<!-- BEGIN GENERATED CODEX RUNTIME TRUTH: do not edit -->
## Runtime truth (generator-owned)

This section is generated from the Codex adapter contract and the target node
contract. Keep the two surfaces distinct:

### Codex adapter transport

- **Command name:** `ticket_pipeline`
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

- **Backing node:** `node_ticket_pipeline`
- **Contract command name:** `ticket_pipeline`
- **Contract command topic:** `onex.cmd.omnimarket.ticket-pipeline-start.v1`
- **Contract terminal topic:** `onex.evt.omnimarket.ticket-pipeline-completed.v1`

The skill must use the request wrapper for dispatch. It must not publish the
target node command topic directly as its generic adapter transport; the target
topics above are contract metadata selected by the runtime adapter.
<!-- END GENERATED CODEX RUNTIME TRUTH -->


| Argument | Description | Default |
| --- | --- | --- |
| `ticket_id` | Linear ticket ID such as `<TICKET-ID>` | Required |
| `correlation_id` | UUID v4 correlation id for the pipeline run | Generate when omitted |
| `skip_test_iterate` | Skip the TEST_ITERATE phase | `false` |
| `dry_run` | Run without side effects | `false` |
| `skip_to` | Resume phase for the bounded pipeline slice | Optional |
| `requested_at` | ISO-8601 request timestamp | Current UTC time |
| `target_runtime_address` | Optional `runtime://...` runtime target | Uses `ONEX_TARGET_RUNTIME_ADDRESS` when set |

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH uv run python scripts/run_codex_runtime_request.py \
  --command-name "ticket_pipeline" \
  --payload '<json-payload>' \
  --timeout-ms 600000
```

If the user supplies a `runtime://...` target, add
`--target-runtime-address '<runtime-address>'` to the request command. If the
argument is omitted, the wrapper uses `ONEX_TARGET_RUNTIME_ADDRESS` when set.

For event-bus-free preflight, add `--compile-only`. This validates the payload,
command topic, response topic, correlation id, timeout, and target runtime
address without publishing to Kafka or starting a runtime.

Build the payload with this shape:

```json
{
  "correlation_id": "<uuid-v4>",
  "ticket_id": "<TICKET-ID>",
  "skip_test_iterate": false,
  "dry_run": true,
  "requested_at": "<utc-iso-8601-timestamp>"
}
```

Map user inputs into the same field names. Generate `correlation_id` and
`requested_at` when the user does not supply them. Only include `skip_to` when
the user explicitly asks to resume from a valid phase such as `pre_flight`,
`implement`, or `local_review`.

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_ticket_pipeline/`
- Codex adapter request wrapper: `scripts/run_codex_runtime_request.py`
- Command name: `ticket_pipeline`
- Target contract command topic: `onex.cmd.omnimarket.ticket-pipeline-start.v1`
- Target contract terminal topic: `onex.evt.omnimarket.ticket-pipeline-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `stop_reason`, `ran_phase`, `phase_results`,
and the nested `completed` event. Treat `stop_reason: not_implemented` at
`local_review` as the current bounded-slice stop state rather than a runtime
failure.
