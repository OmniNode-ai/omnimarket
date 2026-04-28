# Session Orchestrator — Codex Instructions

You have access to the OmniMarket `node_session_orchestrator` node through the local runtime
ingress client. When the user asks you to unified session orchestrator — health gate → rsd scoring → dispatch (omn-8367 poc), use this
procedure. **Do not implement the logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| session_id | sess-{date}-{time} identifier. Auto-generated if not provided. | — |
| mode | Execution mode. interactive awaits user approval after Phase 1. | interactive |
| dry_run | Print plan without dispatching workers. | False |
| skip_health | Skip Phase 1 health gate. Emergency use only. | False |
| standing_orders_path |  | .onex_state/session/standing_orders.json |
| state_dir |  | .onex_state/session |
| phase | Run only a specific phase (1/2/3). 0 = all phases. | 0 |

## Procedure

### Step 1 — Assemble payload

Build a JSON payload from the user's request:

```json
{
  "correlation_id": "<generate a UUID v4>"
}
```

Only include fields the user explicitly specified. The node applies defaults for
omitted fields.

### Step 2 — Dispatch through the local runtime client

Run:

```bash
env -u PYTHONPATH /opt/homebrew/bin/python3.13 scripts/run_codex_runtime_request.py \
  --node-alias "session_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

Notes:
- `scripts/run_codex_runtime_request.py` is the supported repo-local request wrapper.
- `session_orchestrator` resolves through the runtime ingress route table.
- The command prints a JSON response object to stdout.

### Step 3 — Interpret the response

If `ok` is `true`, render `dispatch_result` clearly for the user.

If `ok` is `false`, surface `error.code` and `error.message` directly.

If the dry run depends on GitHub or Linear and those systems are unreachable,
report that degraded condition explicitly rather than inventing inventory or
ticket state.

### Step 4 — Format output

On success: render the runtime `dispatch_result` in a clear format for the user.

On timeout: report that the operation timed out.

On error: surface the runtime ingress error code and message.

## Important

Do not implement any business logic. All processing runs in the OmniMarket
`node_session_orchestrator` node. These instructions only cover runtime ingress dispatch and
output formatting.
