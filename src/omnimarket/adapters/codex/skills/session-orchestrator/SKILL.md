---
name: session-orchestrator
description: Thin Codex skill shim for node_session_orchestrator. Use to run the session health gate, queue scoring, and dispatch planning loop.
---

# Session Orchestrator

This skill is a thin runtime-ingress shim over the OmniMarket
`node_session_orchestrator` node. The node owns health gating, queue scoring,
and dispatch planning. Do not add local health probes, dispatch compilation, or
ticket triage logic to this skill.

## Arguments

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

## Dispatch

Run from the `omnimarket` repo or an `omnimarket` worktree:

```bash
env -u PYTHONPATH /opt/homebrew/bin/python3.13 scripts/run_codex_runtime_request.py \
  --node-alias "session_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

Map user inputs into a JSON payload using the same field names:

- `correlation_id`
- `session_id`
- `mode`
- `dry_run`
- `skip_health`
- `standing_orders_path`
- `state_dir`
- `phase`

If `ok` is `true` and `output_payloads` is present, treat `output_payloads[0]`
as the primary node result.

If `ok` is `false`, surface `error.code` and `error.message` directly.

## Contract

- Backing node: `src/omnimarket/nodes/node_session_orchestrator/`
- Local request wrapper: `scripts/run_codex_runtime_request.py`
- Route alias: `session_orchestrator`
- Runtime topic: `onex.cmd.omnimarket.session-orchestrator-start.v1`
- Completion topic: `onex.evt.omnimarket.session-orchestrator-completed.v1`

## Output

Prefer `output_payloads[0]`. Render the node result with `status`,
`halt_reason`, `health_report`, `dispatch_queue`, and `dispatch_receipts`. For
dry runs, report queue length and receipt count without inventing worker
execution details.
