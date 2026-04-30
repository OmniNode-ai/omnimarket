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

| Argument | Description | Default |
| --- | --- | --- |
| `ticket_id` | Linear ticket ID such as `OMN-10400` | Required |
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
  "ticket_id": "OMN-10400",
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
- Runtime topic: `onex.cmd.omnimarket.ticket-pipeline-start.v1`
- Completion topic: `onex.evt.omnimarket.ticket-pipeline-completed.v1`

## Output

Prefer `output_payloads[0]`. Render `stop_reason`, `ran_phase`, `phase_results`,
and the nested `completed` event. Treat `stop_reason: not_implemented` at
`local_review` as the current bounded-slice stop state rather than a runtime
failure.
