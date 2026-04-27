# PR Lifecycle — Codex Instructions

You have access to the OmniMarket `node_pr_lifecycle_orchestrator` node through the local runtime
ingress client. When the user asks you to pr lifecycle orchestrator — fsm orchestrator wiring 5 sub-handlers (inventory, triage, merge, fix, reducer), use this
procedure. **Do not implement the logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| dry_run | Run without side effects | False |
| inventory_only | Stop after inventory; no triage, merge, or fix | False |
| fix_only | Only fix non-green PRs; skip merge | False |
| merge_only | Only merge green PRs; skip fix | False |
| repos | Comma-separated repo slugs to filter (empty = all) |  |
| enable_auto_rebase | Auto-rebase stale (Track A-update) PR branches before merge | True |
| verify | Run verification_sweep per-PR as pre-merge gate (OMN-7742) | False |
| verify_timeout_seconds | Hard per-PR verification timeout in seconds | 30 |

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
  --node-alias "pr_lifecycle_orchestrator" \
  --payload '<json-payload>' \
  --timeout-ms 300000
```

Notes:
- `scripts/run_codex_runtime_request.py` is the supported repo-local request wrapper.
- `pr_lifecycle_orchestrator` resolves through the runtime ingress route table.
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
`node_pr_lifecycle_orchestrator` node. These instructions only cover runtime ingress dispatch and
output formatting.
