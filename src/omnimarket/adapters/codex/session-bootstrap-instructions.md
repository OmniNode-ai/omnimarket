# Session Bootstrap — Codex Instructions

You have access to the OmniMarket `node_session_bootstrap` node through the local runtime
ingress client. When the user asks you to overnight session bootstrapper — reads contract, writes snapshot, configures timers, use this
procedure. **Do not implement the logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| session_id |  | — |
| session_mode | Controls which crons are activated and what halt conditions apply | — |
| active_sprint_id | Linear cycle ID, or 'auto-detect' to query Linear for active sprint | auto-detect |
| model_routing_preference | Routing preference passed to dogfood gate at dispatch time | local-first |
| contract | ModelSessionContract -- session-level verification contract | — |
| state_dir |  | .onex_state |
| dry_run |  | False |

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
env -u PYTHONPATH uv run scripts/run_codex_runtime_request.py \
  --node-alias "session_bootstrap" \
  --payload '<json-payload>' \
  --timeout-ms 30000
```

Notes:
- `scripts/run_codex_runtime_request.py` is the supported repo-local request wrapper.
- `session_bootstrap` resolves through the runtime ingress route table.
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
`node_session_bootstrap` node. These instructions only cover runtime ingress dispatch and
output formatting.
