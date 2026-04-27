# AI Slop Sweep — Codex Instructions

You have access to the OmniMarket `node_aislop_sweep` node through the local
runtime ingress client. When the user asks you to scan for AI-generated quality
anti-patterns, code quality issues, or "AI slop", use this procedure. **Do not
implement scanning logic yourself.**

## Supported arguments

| Argument | Description | Default |
|----------|-------------|---------|
| repos | Comma-separated repo names to scan | All supported repos |
| checks | Pattern categories: phantom-callables, compat-shims, prohibited-patterns, hardcoded-topics, todo-fixme, todo-stale, empty-impls | All |
| dry_run | Scan and report only — no tickets, no fixes | false |
| ticket | Create Linear tickets for findings | false |
| auto_fix | Attempt auto-fix for trivial patterns | false |
| severity_threshold | Minimum severity: WARNING or ERROR | WARNING |

## Procedure

### Step 1 — Assemble payload

Build a JSON payload from the user's request:

```json
{
  "correlation_id": "<generate a UUID v4>",
  "repos": ["omniclaude", "omnibase_core"],
  "checks": ["phantom-callables", "todo-fixme"],
  "dry_run": true,
  "ticket": false,
  "auto_fix": false,
  "severity_threshold": "WARNING"
}
```

Only include fields the user explicitly specified. The node applies defaults for
omitted fields.

### Step 2 — Publish command event

Run:

```bash
env -u PYTHONPATH /opt/homebrew/bin/python3.13 scripts/run_codex_runtime_request.py \
  --node-alias "aislop_sweep" \
  --payload '<json-payload>' \
  --timeout-ms 120000
```

The command prints a JSON response object to stdout.

### Step 3 — Interpret the response

If `ok` is `true`, render `dispatch_result` as a summary with findings grouped
by severity and check category. Include total counts and per-repo breakdowns.

If `ok` is `false`, surface `error.code` and `error.message` directly.

### Step 4 — Format output

On success: render the runtime `dispatch_result` as a summary with findings
grouped by severity and check category. Include total counts and per-repo
breakdowns.

On timeout: report that the sweep timed out after 120 seconds.

On error: surface the runtime ingress error code and message.

## Important

Do not implement any scanning, triaging, or ticket-creation logic. All business
logic runs in the OmniMarket `aislop_sweep` node. These instructions only cover
runtime ingress dispatch and output formatting.
